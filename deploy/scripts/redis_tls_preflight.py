#!/usr/bin/env python3
"""Read-only, fail-closed validation for isolated production Redis TLS files."""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

CA_PATH = Path("/etc/sms-platform/redis-tls/ca.pem")
CERTIFICATE_PATH = Path("/etc/sms-platform/redis-tls/server.pem")
PRIVATE_KEY_PATH = Path(__file__).resolve().parents[1] / "secrets" / "redis_tls_server_key"
OPENSSL_BINARY = Path("/usr/bin/openssl")
REQUIRED_SERVER_NAMES = frozenset({"redis", "redis-auth", "redis-control"})
MAX_PUBLIC_FILE_BYTES = 1024 * 1024
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_OPENSSL_OUTPUT_BYTES = 256 * 1024
OPENSSL_TIMEOUT_SECONDS = 10
MINIMUM_CERTIFICATE_REMAINING = timedelta(days=7)
_DNS_SAN_PATTERN = re.compile(rb"(?:^|[\s,])DNS:([^\s,]+)")
_PRIVATE_PEM_BLOCK_PATTERN = re.compile(
    b"-----" + rb"BEGIN (?:[A-Z0-9]+ )?" + b"PRIVATE " + b"KEY-----"
)
_SERVER_AUTH_OID = b".".join(
    (b"1", b"3", b"6", b"1", b"5", b"5", b"7", b"3", b"1")
)


class RedisTLSPreflightError(RuntimeError):
    """A safe, operator-facing Redis TLS preflight failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    """A securely opened file that remains pinned for OpenSSL validation."""

    label: str
    descriptor: int


@dataclass(frozen=True, slots=True)
class RedisTLSPreflightReport:
    checks: tuple[str, ...]
    san_names: tuple[str, ...]


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


@contextlib.contextmanager
def _open_validated_file(
    path: Path,
    *,
    label: str,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    maximum_bytes: int,
) -> Iterator[ValidatedFile]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RedisTLSPreflightError(
            f"{label}_file_contract", f"{label} file is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RedisTLSPreflightError(
            f"{label}_file_contract", f"{label} must be a non-symlink regular file"
        )
    if (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid):
        raise RedisTLSPreflightError(
            f"{label}_file_contract", f"{label} owner must match the production contract"
        )
    if _mode(metadata) != expected_mode:
        raise RedisTLSPreflightError(
            f"{label}_file_contract", f"{label} mode must be {expected_mode:04o}"
        )
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise RedisTLSPreflightError(
            f"{label}_file_contract", f"{label} size violates the production contract"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RedisTLSPreflightError(
            f"{label}_file_contract", f"{label} cannot be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or (opened.st_uid, opened.st_gid) != (expected_uid, expected_gid)
            or _mode(opened) != expected_mode
            or opened.st_size <= 0
            or opened.st_size > maximum_bytes
        ):
            raise RedisTLSPreflightError(
                f"{label}_file_contract", f"{label} changed during validation"
            )
        yield ValidatedFile(label=label, descriptor=descriptor)
    finally:
        os.close(descriptor)


def _descriptor_path(descriptor: int) -> str:
    if Path("/proc/self/fd").is_dir():
        return f"/proc/self/fd/{descriptor}"
    if Path("/dev/fd").is_dir():
        return f"/dev/fd/{descriptor}"
    raise RedisTLSPreflightError(
        "descriptor_interface_unavailable",
        "the operating system cannot expose pinned TLS files to OpenSSL",
    )


def _assert_public_pem_contains_no_private_key(file: ValidatedFile) -> None:
    """公开 CA/证书挂载不得夹带可被任一客户端读取的签发或服务私钥。"""

    try:
        os.lseek(file.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(file.descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PUBLIC_FILE_BYTES:
                raise RedisTLSPreflightError(
                    f"{file.label}_file_contract",
                    f"{file.label} size violates the production contract",
                )
            chunks.append(chunk)
    except OSError as error:
        raise RedisTLSPreflightError(
            f"{file.label}_file_contract",
            f"{file.label} could not be inspected safely",
        ) from error
    if _PRIVATE_PEM_BLOCK_PATTERN.search(b"".join(chunks)) is not None:
        raise RedisTLSPreflightError(
            "public_pem_contains_private_key",
            "public Redis TLS material contains a private key block",
        )


def _run_openssl(
    binary: Path,
    arguments: Sequence[str],
    descriptors: Sequence[int],
    *,
    failure_code: str,
    failure_message: str,
) -> bytes:
    if not binary.is_absolute():
        raise RedisTLSPreflightError(
            "openssl_unavailable", "OpenSSL must be invoked by an absolute path"
        )
    try:
        for descriptor in descriptors:
            os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise RedisTLSPreflightError(
            "tls_file_contract_changed", "a pinned TLS file could not be rewound safely"
        ) from error
    command = [str(binary), *arguments]
    try:
        completed: subprocess.CompletedProcess[bytes] = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            close_fds=True,
            pass_fds=tuple(descriptors),
            timeout=OPENSSL_TIMEOUT_SECONDS,
            env={"LC_ALL": "C"},
        )
    except FileNotFoundError as error:
        raise RedisTLSPreflightError(
            "openssl_unavailable", "OpenSSL is unavailable at the approved path"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RedisTLSPreflightError(
            "openssl_timeout", "OpenSSL validation exceeded the bounded timeout"
        ) from error
    except OSError as error:
        raise RedisTLSPreflightError(
            "openssl_unavailable", "OpenSSL could not be executed safely"
        ) from error
    if completed.returncode != 0:
        raise RedisTLSPreflightError(failure_code, failure_message)
    if len(completed.stdout) > MAX_OPENSSL_OUTPUT_BYTES:
        raise RedisTLSPreflightError(
            "openssl_output_invalid", "OpenSSL returned an unexpectedly large result"
        )
    return completed.stdout


def _parse_certificate_time(value: bytes, prefix: bytes) -> datetime:
    for line in value.splitlines():
        if line.startswith(prefix):
            encoded = line[len(prefix) :].decode("ascii", errors="strict")
            parsed = datetime.strptime(encoded, "%b %d %H:%M:%S %Y GMT")
            return parsed.replace(tzinfo=UTC)
    raise RedisTLSPreflightError(
        "certificate_time_invalid", "certificate validity could not be interpreted"
    )


def validate_redis_tls(
    *,
    ca_path: Path = CA_PATH,
    certificate_path: Path = CERTIFICATE_PATH,
    private_key_path: Path = PRIVATE_KEY_PATH,
    expected_uid: int = 0,
    expected_gid: int = 0,
    openssl_binary: Path = OPENSSL_BINARY,
    now: datetime | None = None,
) -> RedisTLSPreflightReport:
    """Validate the fixed Redis TLS contract without modifying host state."""

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise RedisTLSPreflightError(
            "preflight_clock_invalid", "the validation clock must include a timezone"
        )

    with contextlib.ExitStack() as stack:
        ca = stack.enter_context(
            _open_validated_file(
                ca_path,
                label="ca",
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                expected_mode=0o644,
                maximum_bytes=MAX_PUBLIC_FILE_BYTES,
            )
        )
        certificate = stack.enter_context(
            _open_validated_file(
                certificate_path,
                label="certificate",
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                expected_mode=0o644,
                maximum_bytes=MAX_PUBLIC_FILE_BYTES,
            )
        )
        private_key = stack.enter_context(
            _open_validated_file(
                private_key_path,
                label="private_key",
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                expected_mode=0o600,
                maximum_bytes=MAX_PRIVATE_KEY_BYTES,
            )
        )
        _assert_public_pem_contains_no_private_key(ca)
        _assert_public_pem_contains_no_private_key(certificate)
        ca_argument = _descriptor_path(ca.descriptor)
        certificate_argument = _descriptor_path(certificate.descriptor)
        private_key_argument = _descriptor_path(private_key.descriptor)

        validity = _run_openssl(
            openssl_binary,
            ["x509", "-in", certificate_argument, "-noout", "-startdate", "-enddate"],
            [certificate.descriptor],
            failure_code="certificate_time_invalid",
            failure_message="certificate validity could not be read",
        )
        try:
            not_before = _parse_certificate_time(validity, b"notBefore=")
            not_after = _parse_certificate_time(validity, b"notAfter=")
        except (UnicodeDecodeError, ValueError) as error:
            raise RedisTLSPreflightError(
                "certificate_time_invalid", "certificate validity could not be interpreted"
            ) from error
        if current_time < not_before or current_time > not_after:
            raise RedisTLSPreflightError(
                "certificate_not_current", "certificate is not currently valid"
            )
        if not_after - current_time < MINIMUM_CERTIFICATE_REMAINING:
            raise RedisTLSPreflightError(
                "certificate_expires_too_soon",
                "certificate has less than seven days of validity remaining",
            )

        extended_key_usage = _run_openssl(
            openssl_binary,
            ["x509", "-in", certificate_argument, "-noout", "-ext", "extendedKeyUsage"],
            [certificate.descriptor],
            failure_code="server_auth_eku_missing",
            failure_message="certificate serverAuth EKU is missing",
        )
        if (
            b"TLS Web Server Authentication" not in extended_key_usage
            and _SERVER_AUTH_OID not in extended_key_usage
        ):
            raise RedisTLSPreflightError(
                "server_auth_eku_missing", "certificate serverAuth EKU is missing"
            )

        subject_alternative_names = _run_openssl(
            openssl_binary,
            ["x509", "-in", certificate_argument, "-noout", "-ext", "subjectAltName"],
            [certificate.descriptor],
            failure_code="san_contract_invalid",
            failure_message="certificate SAN extension is unavailable",
        )
        try:
            san_names = {
                match.decode("ascii", errors="strict").lower()
                for match in _DNS_SAN_PATTERN.findall(subject_alternative_names)
            }
        except UnicodeDecodeError as error:
            raise RedisTLSPreflightError(
                "san_contract_invalid", "certificate SAN extension is invalid"
            ) from error
        if not REQUIRED_SERVER_NAMES.issubset(san_names):
            raise RedisTLSPreflightError(
                "san_contract_invalid",
                "certificate SAN does not cover every Redis service name",
            )
        for server_name in sorted(REQUIRED_SERVER_NAMES):
            _run_openssl(
                openssl_binary,
                ["x509", "-in", certificate_argument, "-noout", "-checkhost", server_name],
                [certificate.descriptor],
                failure_code="hostname_verification_failed",
                failure_message="certificate hostname verification failed",
            )

        _run_openssl(
            openssl_binary,
            [
                "verify",
                "-purpose",
                "sslserver",
                "-CAfile",
                ca_argument,
                certificate_argument,
            ],
            [ca.descriptor, certificate.descriptor],
            failure_code="ca_chain_invalid",
            failure_message="certificate chain or server purpose validation failed",
        )

        certificate_public_key = _run_openssl(
            openssl_binary,
            ["x509", "-in", certificate_argument, "-pubkey", "-noout"],
            [certificate.descriptor],
            failure_code="certificate_public_key_invalid",
            failure_message="certificate public key could not be read",
        ).strip()
        private_public_key = _run_openssl(
            openssl_binary,
            [
                "pkey",
                "-in",
                private_key_argument,
                "-passin",
                "pass:",
                "-pubout",
            ],
            [private_key.descriptor],
            failure_code="private_key_invalid",
            failure_message="private key is invalid or encrypted",
        ).strip()
        if not certificate_public_key or not hmac.compare_digest(
            certificate_public_key, private_public_key
        ):
            raise RedisTLSPreflightError(
                "key_certificate_mismatch", "private key does not match the certificate"
            )

    return RedisTLSPreflightReport(
        checks=(
            "file_contract",
            "public_pem_separation",
            "certificate_current",
            "server_auth_eku",
            "service_sans",
            "ca_chain",
            "key_pair",
        ),
        san_names=tuple(sorted(REQUIRED_SERVER_NAMES)),
    )


def _emit_result(payload: dict[str, object], *, stream: TextIO = sys.stdout) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=stream)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=PRIVATE_KEY_PATH.parent,
        help="platform secrets directory containing redis_tls_server_key",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = validate_redis_tls(
            private_key_path=arguments.secrets_dir / PRIVATE_KEY_PATH.name
        )
    except RedisTLSPreflightError as error:
        _emit_result(
            {
                "code": error.code,
                "event": "redis_tls_preflight_result",
                "message": error.message,
                "status": "failed",
            },
            stream=sys.stderr,
        )
        return 1
    except Exception:
        _emit_result(
            {
                "code": "unexpected_preflight_failure",
                "event": "redis_tls_preflight_result",
                "message": "Redis TLS preflight failed unexpectedly",
                "status": "failed",
            },
            stream=sys.stderr,
        )
        return 1
    _emit_result(
        {
            "checks": list(report.checks),
            "event": "redis_tls_preflight_result",
            "san_names": list(report.san_names),
            "status": "passed",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
