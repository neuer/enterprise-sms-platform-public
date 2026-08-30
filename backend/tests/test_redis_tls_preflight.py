from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "redis_tls_preflight.py"
HEALTHCHECK = ROOT / "deploy" / "redis-domain-healthcheck.sh"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("redis_tls_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module() -> ModuleType:
    return load_module()


@dataclass(frozen=True, slots=True)
class TLSFixture:
    ca_path: Path
    certificate_path: Path
    private_key_path: Path
    ca_key: rsa.RSAPrivateKey
    ca_name: x509.Name
    server_key: rsa.RSAPrivateKey


def _certificate_authority(
    *,
    common_name: str = "SMS Redis test CA",
) -> tuple[rsa.RSAPrivateKey, x509.Name, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, name, certificate


def _server_certificate(
    *,
    ca_key: rsa.RSAPrivateKey,
    ca_name: x509.Name,
    server_key: rsa.RSAPrivateKey,
    san_names: tuple[str, ...] = ("redis", "redis-auth", "redis-control"),
    include_server_auth: bool = True,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "redis")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(hours=1))
        .not_valid_after(not_after or now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in san_names]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if include_server_auth:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    return builder.sign(ca_key, hashes.SHA256())


def _write_tls_fixture(
    tmp_path: Path,
    *,
    san_names: tuple[str, ...] = ("redis", "redis-auth", "redis-control"),
    include_server_auth: bool = True,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> TLSFixture:
    ca_key, ca_name, ca_certificate = _certificate_authority()
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_certificate = _server_certificate(
        ca_key=ca_key,
        ca_name=ca_name,
        server_key=server_key,
        san_names=san_names,
        include_server_auth=include_server_auth,
        not_before=not_before,
        not_after=not_after,
    )
    ca_path = tmp_path / "ca.pem"
    certificate_path = tmp_path / "server.pem"
    private_key_path = tmp_path / "redis_tls_server_key"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ca_path.chmod(0o644)
    certificate_path.chmod(0o644)
    private_key_path.chmod(0o600)
    return TLSFixture(
        ca_path=ca_path,
        certificate_path=certificate_path,
        private_key_path=private_key_path,
        ca_key=ca_key,
        ca_name=ca_name,
        server_key=server_key,
    )


def _openssl() -> Path:
    binary = shutil.which("openssl")
    if binary is None:
        pytest.skip("OpenSSL is required for the Redis TLS preflight contract test")
    return Path(binary).resolve()


class TLSReport(Protocol):
    checks: tuple[str, ...]
    san_names: tuple[str, ...]


def _validate(module: ModuleType, fixture: TLSFixture) -> TLSReport:
    return cast(
        TLSReport,
        module.validate_redis_tls(
            ca_path=fixture.ca_path,
            certificate_path=fixture.certificate_path,
            private_key_path=fixture.private_key_path,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            openssl_binary=_openssl(),
        ),
    )


def test_valid_ca_certificate_sans_eku_and_key_pair_pass(
    module: ModuleType, tmp_path: Path
) -> None:
    report = _validate(module, _write_tls_fixture(tmp_path))

    assert report.checks == (
        "file_contract",
        "public_pem_separation",
        "certificate_current",
        "server_auth_eku",
        "service_sans",
        "ca_chain",
        "key_pair",
    )
    assert report.san_names == ("redis", "redis-auth", "redis-control")


@pytest.mark.parametrize(
    ("target", "mode", "expected_code"),
    (
        ("ca_path", 0o600, "ca_file_contract"),
        ("certificate_path", 0o600, "certificate_file_contract"),
        ("private_key_path", 0o644, "private_key_file_contract"),
    ),
)
def test_exact_owner_mode_contract_fails_closed(
    module: ModuleType,
    tmp_path: Path,
    target: str,
    mode: int,
    expected_code: str,
) -> None:
    fixture = _write_tls_fixture(tmp_path)
    getattr(fixture, target).chmod(mode)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == expected_code


def test_production_default_requires_root_owned_files(module: ModuleType, tmp_path: Path) -> None:
    if os.getuid() == 0 and os.getgid() == 0:
        pytest.skip("the test process already has the production owner")
    fixture = _write_tls_fixture(tmp_path)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        module.validate_redis_tls(
            ca_path=fixture.ca_path,
            certificate_path=fixture.certificate_path,
            private_key_path=fixture.private_key_path,
            openssl_binary=_openssl(),
        )

    assert caught.value.code == "ca_file_contract"


def test_symlink_is_rejected_before_openssl(module: ModuleType, tmp_path: Path) -> None:
    fixture = _write_tls_fixture(tmp_path)
    link = tmp_path / "linked-server.pem"
    link.symlink_to(fixture.certificate_path)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        module.validate_redis_tls(
            ca_path=fixture.ca_path,
            certificate_path=link,
            private_key_path=fixture.private_key_path,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            openssl_binary=_openssl(),
        )

    assert caught.value.code == "certificate_file_contract"


def test_certificate_must_be_current(module: ModuleType, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    fixture = _write_tls_fixture(
        tmp_path,
        not_before=now - timedelta(days=2),
        not_after=now - timedelta(days=1),
    )

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "certificate_not_current"


def test_certificate_requires_at_least_seven_days_remaining(
    module: ModuleType, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    fixture = _write_tls_fixture(
        tmp_path,
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=6),
    )

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "certificate_expires_too_soon"


def test_server_auth_eku_is_mandatory(module: ModuleType, tmp_path: Path) -> None:
    fixture = _write_tls_fixture(tmp_path, include_server_auth=False)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "server_auth_eku_missing"


def test_san_must_cover_all_three_service_names(module: ModuleType, tmp_path: Path) -> None:
    fixture = _write_tls_fixture(tmp_path, san_names=("redis", "redis-auth"))

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "san_contract_invalid"


def test_certificate_chain_must_validate(module: ModuleType, tmp_path: Path) -> None:
    fixture = _write_tls_fixture(tmp_path)
    _, _, unrelated_ca = _certificate_authority(common_name="Unrelated test CA")
    fixture.ca_path.write_bytes(unrelated_ca.public_bytes(serialization.Encoding.PEM))
    fixture.ca_path.chmod(0o644)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "ca_chain_invalid"


def test_private_key_must_match_certificate(module: ModuleType, tmp_path: Path) -> None:
    fixture = _write_tls_fixture(tmp_path)
    unrelated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fixture.private_key_path.write_bytes(
        unrelated_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    fixture.private_key_path.chmod(0o600)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "key_certificate_mismatch"


@pytest.mark.parametrize("public_target", ("ca_path", "certificate_path"))
def test_public_pem_files_must_not_contain_any_private_key_block(
    module: ModuleType,
    tmp_path: Path,
    public_target: str,
) -> None:
    fixture = _write_tls_fixture(tmp_path)
    target = cast(Path, getattr(fixture, public_target))
    target.write_bytes(
        target.read_bytes()
        + fixture.ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    target.chmod(0o644)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "public_pem_contains_private_key"


def test_encrypted_private_key_is_rejected_without_prompting(
    module: ModuleType, tmp_path: Path
) -> None:
    fixture = _write_tls_fixture(tmp_path)
    fixture.private_key_path.write_bytes(
        fixture.server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"never-print-this-password"),
        )
    )
    fixture.private_key_path.chmod(0o600)

    with pytest.raises(module.RedisTLSPreflightError) as caught:
        _validate(module, fixture)

    assert caught.value.code == "private_key_invalid"
    assert "never-print-this-password" not in str(caught.value)


def test_cli_failure_is_structured_and_does_not_echo_internal_exception(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "private-key-material-must-never-be-printed"

    def fail(**_kwargs: object) -> object:
        raise ValueError(marker)

    monkeypatch.setattr(module, "validate_redis_tls", fail)

    assert module.main([]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert captured.out == ""
    assert payload == {
        "code": "unexpected_preflight_failure",
        "event": "redis_tls_preflight_result",
        "message": "Redis TLS preflight failed unexpectedly",
        "status": "failed",
    }
    assert marker not in captured.err


def test_implementation_is_read_only_and_uses_safe_openssl_argv() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for mutation in (
        ".chmod(",
        ".chown(",
        ".mkdir(",
        "Path.replace(",
        ".rename(",
        ".unlink(",
        "os.remove(",
        "shutil.",
        "shell=True",
    ):
        assert mutation not in source
    assert "subprocess.run(" in source
    assert "stdin=subprocess.DEVNULL" in source
    assert "capture_output=True" in source
    assert "pass_fds=tuple(descriptors)" in source
    assert "completed.stderr" not in source


def test_tls_healthcheck_uses_service_name_for_connection_and_sni() -> None:
    source = HEALTHCHECK.read_text(encoding="utf-8")

    assert '--sni "$server_name"' in source
    assert '-h "$server_name"' in source
    assert "--cacert /run/redis-tls/ca.pem" in source
    assert source.index('--sni "$server_name"') < source.index('-h "$server_name"')


def test_public_production_paths_stay_fixed_and_private_key_uses_platform_secrets(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Path("/etc/sms-platform/redis-tls/ca.pem") == module.CA_PATH
    assert Path("/etc/sms-platform/redis-tls/server.pem") == module.CERTIFICATE_PATH
    assert ROOT / "deploy" / "secrets" / "redis_tls_server_key" == module.PRIVATE_KEY_PATH
    assert Path("/usr/bin/openssl") == module.OPENSSL_BINARY

    captured: dict[str, Path] = {}

    def validate(**kwargs: Path) -> TLSReport:
        captured.update(kwargs)
        return cast(
            TLSReport,
            module.RedisTLSPreflightReport(checks=("file_contract",), san_names=()),
        )

    monkeypatch.setattr(module, "validate_redis_tls", validate)
    assert module.main(["--secrets-dir", str(tmp_path)]) == 0
    assert captured == {"private_key_path": tmp_path / "redis_tls_server_key"}


def test_file_modes_are_expressed_as_exact_permissions() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert stat.S_IMODE(0o100644) == 0o644
    assert "expected_mode=0o644" in source
    assert "expected_mode=0o600" in source
