#!/usr/bin/env python3
"""为无历史公有快照切换准备一次性的职责凭据与三域 Redis。"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

DEFAULT_ROOT = Path("/opt/sms-platform")
DEFAULT_RUNTIME_ROOT = Path("/run/sms-platform/secrets")
DEFAULT_VENDOR_ROOT = Path("/var/lib/sms-platform/vendor-test/credentials")
HOST_MANIFEST = Path(
    "/usr/local/libexec/sms-platform/test-secure-access/manifest.json"
)
STATE_ROOT = Path("/var/lib/sms-platform/public-cutover-bootstrap")
STATE_FILE = STATE_ROOT / "state.json"
REDIS_IMAGE = "sms-platform-redis:local"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_OLD_SECRET_NAMES = frozenset(
    {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "jwt_secret",
        "ldap_bind_password",
        "db_owner_password",
        "db_app_password",
    }
)
_NEW_SECRET_NAMES = frozenset(
    {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "audit_context_key",
        "audit_system_api_context_key",
        "audit_system_realtime_context_key",
        "audit_system_bulk_context_key",
        "alert_credential_public_key",
        "alert_credential_private_key",
        "jwt_secret",
        "ldap_bind_password",
        "metrics_scrape_token",
        "db_owner_password",
        "db_auth_password",
        "db_accept_password",
        "db_send_password",
        "db_callback_password",
        "db_export_password",
        "db_scheduler_password",
        "db_metrics_password",
        "redis_broker_password",
        "redis_auth_password",
        "redis_control_password",
        "redis_tls_server_key",
    }
)
_GENERATED_SECRET_NAMES = _NEW_SECRET_NAMES - _OLD_SECRET_NAMES
_DEV_SECRET = "dev-apikeys.txt"
_CRYPTO_KEY_NAMES = frozenset(
    {
        "audit_context_key",
        "audit_system_api_context_key",
        "audit_system_realtime_context_key",
        "audit_system_bulk_context_key",
        "alert_credential_public_key",
        "alert_credential_private_key",
    }
)


class PublicCutoverBootstrapError(RuntimeError):
    """一次性基础设施准备未满足安全合同。"""


def _generate_alert_keypair() -> tuple[str, str]:
    """使用 RFC 7748 Montgomery ladder 生成 X25519 原始 keypair。"""

    private_value = bytearray(secrets.token_bytes(32))
    scalar = bytearray(private_value)
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    number = int.from_bytes(scalar, "little")
    modulus = 2**255 - 19
    x_1 = 9
    x_2, z_2 = 1, 0
    x_3, z_3 = x_1, 1
    swap = 0
    for position in range(254, -1, -1):
        bit = (number >> position) & 1
        swap ^= bit
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = bit
        a = (x_2 + z_2) % modulus
        aa = (a * a) % modulus
        b = (x_2 - z_2) % modulus
        bb = (b * b) % modulus
        e = (aa - bb) % modulus
        c = (x_3 + z_3) % modulus
        d = (x_3 - z_3) % modulus
        da = (d * a) % modulus
        cb = (c * b) % modulus
        x_3 = ((da + cb) ** 2) % modulus
        z_3 = (x_1 * ((da - cb) ** 2)) % modulus
        x_2 = (aa * bb) % modulus
        z_2 = (e * (aa + 121665 * e)) % modulus
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2
    public_value = (x_2 * pow(z_2, modulus - 2, modulus)) % modulus
    return (
        base64.b64encode(public_value.to_bytes(32, "little")).decode("ascii"),
        base64.b64encode(private_value).decode("ascii"),
    )


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> str: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        try:
            result = subprocess.run(
                list(argv),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=dict(environment) if environment is not None else None,
            )
        except OSError as error:
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap command is unavailable"
            ) from error
        if result.returncode != 0:
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap command failed"
            )
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap command output is invalid"
            ) from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_directory(path: Path, *, mode: int, expected_uid: int) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise PublicCutoverBootstrapError(
            "public cutover bootstrap directory is unsafe"
        )


def _safe_secret_directory(
    path: Path,
    expected_names: frozenset[str],
    *,
    expected_uid: int,
) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PublicCutoverBootstrapError("authoritative secrets directory is unsafe")
    names = {entry.name for entry in os.scandir(path)}
    allowed = set(expected_names) | {_DEV_SECRET}
    if not set(expected_names).issubset(names) or not names.issubset(allowed):
        raise PublicCutoverBootstrapError("authoritative secrets inventory is invalid")
    for name in names:
        secret = path / name
        secret_metadata = secret.lstat()
        if (
            not stat.S_ISREG(secret_metadata.st_mode)
            or stat.S_ISLNK(secret_metadata.st_mode)
            or secret_metadata.st_uid != expected_uid
            or stat.S_IMODE(secret_metadata.st_mode) != 0o600
            or secret_metadata.st_size <= 0
        ):
            raise PublicCutoverBootstrapError(
                "authoritative secret file is unsafe"
            )


def _safe_regular_file(
    path: Path,
    *,
    expected_uid: int,
    mode: int,
) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size <= 0
    ):
        raise PublicCutoverBootstrapError(
            "public cutover protected file is unsafe"
        )


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublicCutoverBootstrapError(
            "public cutover bootstrap metadata is invalid"
        ) from error
    if type(value) is not dict:
        raise PublicCutoverBootstrapError(
            "public cutover bootstrap metadata is invalid"
        )
    return value


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    payload = (
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_secret(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, f"{value}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PublicCutoverBootstrap:
    """仅为已安装目标 host-control 快照执行一次公有切换准备。"""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_ROOT,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        vendor_root: Path = DEFAULT_VENDOR_ROOT,
        host_manifest: Path = HOST_MANIFEST,
        state_root: Path = STATE_ROOT,
        expected_uid: int = 0,
        confirmed: bool = False,
        runner: CommandRunner | None = None,
        secret_factory: Callable[[], str] | None = None,
    ) -> None:
        self.root = root
        self.runtime_root = runtime_root
        self.vendor_root = vendor_root
        self.host_manifest = host_manifest
        self.state_root = state_root
        self.state_file = state_root / STATE_FILE.name
        self.expected_uid = expected_uid
        self.confirmed = confirmed
        self.runner = runner or SubprocessRunner()
        self.secret_factory = secret_factory or (lambda: secrets.token_urlsafe(48))

    def _command(
        self,
        *argv: str,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        return self.runner.run(argv, environment=environment)

    def _identity(self) -> tuple[str, str]:
        if os.geteuid() != self.expected_uid:
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap requires root"
            )
        manifest = _read_json(self.host_manifest)
        target = manifest.get("source_commit")
        if type(target) is not str or _COMMIT_RE.fullmatch(target) is None:
            raise PublicCutoverBootstrapError(
                "installed host-control commit is invalid"
            )
        base = self._command(
            "/usr/bin/git",
            "-C",
            str(self.root),
            "rev-parse",
            "HEAD",
        )
        public_main = self._command(
            "/usr/bin/git",
            "-C",
            str(self.root),
            "rev-parse",
            "origin/main^{commit}",
        )
        if (
            _COMMIT_RE.fullmatch(base) is None
            or public_main != target
            or base == target
            or self._command(
                "/usr/bin/git",
                "-C",
                str(self.root),
                "status",
                "--porcelain",
            )
        ):
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap repository identity is invalid"
            )
        return base, target

    def _create_source(self, target: str) -> Path:
        source = self.state_root / f"source-{target}"
        if source.exists():
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap source already exists"
            )
        self._command(
            "/usr/bin/git",
            "-C",
            str(self.root),
            "worktree",
            "add",
            "--detach",
            str(source),
            target,
        )
        (source / ".env").symlink_to(self.root / ".env")
        return source

    def _remove_source(self, source: Path) -> None:
        self._command(
            "/usr/bin/git",
            "-C",
            str(self.root),
            "worktree",
            "remove",
            "--force",
            str(source),
        )

    def _running_redis_image(self) -> str:
        containers = self._command(
            "/usr/bin/docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=sms-platform",
            "--filter",
            "label=com.docker.compose.service=redis",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ).splitlines()
        if (
            len(containers) != 1
            or re.fullmatch(r"[0-9a-f]{12,64}", containers[0]) is None
        ):
            raise PublicCutoverBootstrapError(
                "existing Redis container identity is invalid"
            )
        identity = self._command(
            "/usr/bin/docker",
            "inspect",
            "--format",
            (
                '{{index .Config.Labels "com.docker.compose.project"}}|'
                '{{index .Config.Labels "com.docker.compose.service"}}|'
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
            ),
            containers[0],
        )
        if identity != "sms-platform|redis|running|healthy":
            raise PublicCutoverBootstrapError(
                "existing Redis container is not healthy"
            )
        image = self._command(
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{.Image}}",
            containers[0],
        )
        if _IMAGE_ID_RE.fullmatch(image) is None:
            raise PublicCutoverBootstrapError(
                "existing Redis image identity is invalid"
            )
        return image

    def _build_redis(self, source: Path, target: str) -> tuple[str, str]:
        old_image = self._running_redis_image()
        try:
            schema_revision = self._command(
                "/usr/bin/python3",
                "-c",
                (
                    "import sys;"
                    "sys.path.insert(0,sys.argv[1]);"
                    "from check_test_update_migration import find_migration_head;"
                    "from pathlib import Path;"
                    "print(find_migration_head(Path(sys.argv[2])))"
                ),
                str(source / "deploy/scripts"),
                str(source / "backend/migrations/versions"),
            )
            version = (source / "VERSION").read_text(encoding="utf-8").strip()
            self._command(
                "/usr/bin/docker",
                "build",
                "--file",
                str(source / "deploy/redis.Dockerfile"),
                "--build-arg",
                f"APP_VERSION={version}",
                "--build-arg",
                f"GIT_SHA={target}",
                "--build-arg",
                f"SCHEMA_REVISION={schema_revision}",
                "--tag",
                REDIS_IMAGE,
                str(source),
            )
            new_image = self._command(
                "/usr/bin/docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                REDIS_IMAGE,
            )
            if (
                _IMAGE_ID_RE.fullmatch(new_image) is None
                or old_image == new_image
            ):
                raise PublicCutoverBootstrapError(
                    "public cutover Redis image identity is invalid"
                )
            revision = self._command(
                "/usr/bin/docker",
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                new_image,
            )
            if revision != target:
                raise PublicCutoverBootstrapError(
                    "public cutover Redis image revision is invalid"
                )
            return old_image, new_image
        except (OSError, PublicCutoverBootstrapError, ValueError):
            try:
                self._command(
                    "/usr/bin/docker",
                    "image",
                    "tag",
                    old_image,
                    REDIS_IMAGE,
                )
            except PublicCutoverBootstrapError as rollback_error:
                raise PublicCutoverBootstrapError(
                    "public cutover Redis image rollback is incomplete"
                ) from rollback_error
            raise

    def _prepare_secrets(
        self,
        *,
        base: str,
        target: str,
    ) -> tuple[Path, Path]:
        source_secrets = self.root / "deploy/secrets"
        _safe_secret_directory(
            source_secrets,
            _OLD_SECRET_NAMES,
            expected_uid=self.expected_uid,
        )
        backup = self.state_root / f"backup-{base[:12]}-{target[:12]}"
        _safe_directory(
            backup,
            mode=0o700,
            expected_uid=self.expected_uid,
        )
        backup_secrets = backup / "secrets"
        backup_dotenv = backup / "root.env"
        if backup_secrets.exists() or backup_dotenv.exists():
            _safe_secret_directory(
                backup_secrets,
                _OLD_SECRET_NAMES,
                expected_uid=self.expected_uid,
            )
            _safe_regular_file(
                backup_dotenv,
                expected_uid=self.expected_uid,
                mode=0o600,
            )
        else:
            _safe_regular_file(
                self.root / ".env",
                expected_uid=self.expected_uid,
                mode=0o600,
            )
            shutil.copytree(
                source_secrets,
                backup_secrets,
                copy_function=shutil.copy2,
            )
            shutil.copy2(self.root / ".env", backup_dotenv)
            backup_dotenv.chmod(0o600)
            _safe_regular_file(
                backup_dotenv,
                expected_uid=self.expected_uid,
                mode=0o600,
            )
            _safe_secret_directory(
                backup_secrets,
                _OLD_SECRET_NAMES,
                expected_uid=self.expected_uid,
            )
            _fsync_directory(backup)

        new_secrets = self.root / (
            f".env.public-cutover-secrets-new-{target[:12]}"
        )
        old_local = self.root / (
            f".env.public-cutover-secrets-old-{target[:12]}"
        )
        old_secrets = self.state_root / f"old-secrets-{base[:12]}-{target[:12]}"
        if new_secrets.exists() or old_local.exists() or old_secrets.exists():
            raise PublicCutoverBootstrapError(
                "public cutover secrets transition already exists"
            )
        new_secrets.mkdir(mode=0o700)
        new_secrets.chmod(0o700)
        for name in sorted(_NEW_SECRET_NAMES & _OLD_SECRET_NAMES):
            if name == "db_app_password":
                continue
            shutil.copy2(source_secrets / name, new_secrets / name)
        if (source_secrets / _DEV_SECRET).exists():
            shutil.copy2(source_secrets / _DEV_SECRET, new_secrets / _DEV_SECRET)
        generated_values: set[str] = set()
        for name in sorted(_GENERATED_SECRET_NAMES - _CRYPTO_KEY_NAMES):
            value = self.secret_factory()
            if (
                type(value) is not str
                or not 48 <= len(value) <= 172
                or value in generated_values
            ):
                raise PublicCutoverBootstrapError(
                    "generated public cutover secret is invalid"
                )
            generated_values.add(value)
            _write_secret(new_secrets / name, value)
        _write_secret(
            new_secrets / "audit_context_key",
            base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        )
        for domain in ("api", "realtime", "bulk"):
            _write_secret(
                new_secrets / f"audit_system_{domain}_context_key",
                base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
            )
        public_key, private_key = _generate_alert_keypair()
        _write_secret(new_secrets / "alert_credential_public_key", public_key)
        _write_secret(new_secrets / "alert_credential_private_key", private_key)
        _safe_secret_directory(
            new_secrets,
            _NEW_SECRET_NAMES,
            expected_uid=self.expected_uid,
        )
        os.rename(source_secrets, old_local)
        os.rename(new_secrets, source_secrets)
        _fsync_directory(source_secrets.parent)
        shutil.move(str(old_local), str(old_secrets))
        _safe_secret_directory(
            old_secrets,
            _OLD_SECRET_NAMES,
            expected_uid=self.expected_uid,
        )
        _fsync_directory(self.state_root)
        return backup, old_secrets

    def _runtime_target(self, source: Path) -> str:
        return self._command(
            "/usr/bin/python3",
            str(source / "deploy/scripts/prepare_runtime_secrets.py"),
            "current-target",
            "--runtime-root",
            str(self.runtime_root),
        )

    def _prepare_runtime(self, source: Path) -> None:
        command = [
            "/usr/bin/python3",
            str(source / "deploy/scripts/prepare_runtime_secrets.py"),
        ]
        if (self.vendor_root / "active").is_file():
            command.extend(
                [
                    "prepare",
                    "--source-dir",
                    str(self.root / "deploy/secrets"),
                    "--runtime-root",
                    str(self.runtime_root),
                    "--mode",
                    "development",
                    "--vendor-credential-root",
                    str(self.vendor_root),
                ]
            )
        else:
            command.extend(
                [
                    "revoke-vendor",
                    "--source-dir",
                    str(self.root / "deploy/secrets"),
                    "--runtime-root",
                    str(self.runtime_root),
                    "--mode",
                    "development",
                ]
            )
        self._command(*command)

    def _start_transition_redis(self, source: Path) -> None:
        environment = dict(os.environ)
        environment.update(
            {
                "SMS_RUNTIME_SECRETS_DIR": str(self.runtime_root / "current"),
                "SMS_REDIS_IMAGE": REDIS_IMAGE,
            }
        )
        self._command(
            "/usr/bin/docker",
            "compose",
            "--project-name",
            "sms-platform",
            "--project-directory",
            str(self.root / "deploy"),
            "--env-file",
            str(self.root / ".env"),
            "-f",
            str(source / "deploy/docker-compose.yml"),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "120",
            "redis-auth",
            "redis-control",
            environment=environment,
        )

    def _stop_transition_redis(self, source: Path) -> None:
        environment = dict(os.environ)
        environment.update(
            {
                "SMS_RUNTIME_SECRETS_DIR": str(self.runtime_root / "current"),
                "SMS_REDIS_IMAGE": REDIS_IMAGE,
            }
        )
        self._command(
            "/usr/bin/docker",
            "compose",
            "--project-name",
            "sms-platform",
            "--project-directory",
            str(self.root / "deploy"),
            "--env-file",
            str(self.root / ".env"),
            "-f",
            str(source / "deploy/docker-compose.yml"),
            "stop",
            "redis-auth",
            "redis-control",
            environment=environment,
        )

    def _restore_runtime(self, source: Path, target: str) -> None:
        self._command(
            "/usr/bin/python3",
            str(source / "deploy/scripts/prepare_runtime_secrets.py"),
            "activate",
            "--runtime-root",
            str(self.runtime_root),
            "--target",
            target,
        )

    def _restore_secrets(self, *, base: str, target: str) -> None:
        active = self.root / "deploy/secrets"
        old_local = self.root / (
            f".env.public-cutover-secrets-old-{target[:12]}"
        )
        staged = self.root / (
            f".env.public-cutover-secrets-new-{target[:12]}"
        )
        failed_root = self.state_root / (
            f"failed-secrets-{target[:12]}-{secrets.token_hex(4)}"
        )
        active_is_old = False
        if active.exists():
            try:
                _safe_secret_directory(
                    active,
                    _OLD_SECRET_NAMES,
                    expected_uid=self.expected_uid,
                )
                active_is_old = True
            except PublicCutoverBootstrapError:
                active_is_old = False
        candidates: dict[str, Path] = {
            "attempt-staged": staged,
            "original-old": old_local,
        }
        if not active_is_old:
            candidates["attempt-active"] = active
        present = {
            name: candidate
            for name, candidate in candidates.items()
            if candidate.exists()
        }
        if present:
            failed_root.mkdir(mode=0o700)
            for name, candidate in present.items():
                shutil.move(str(candidate), str(failed_root / name))
            _fsync_directory(failed_root)
        if active_is_old:
            return
        backup = self.state_root / f"backup-{base[:12]}-{target[:12]}" / "secrets"
        _safe_secret_directory(
            backup,
            _OLD_SECRET_NAMES,
            expected_uid=self.expected_uid,
        )
        restore = self.root / (
            f".env.public-cutover-secrets-restore-{target[:12]}"
        )
        if restore.exists() or active.exists():
            raise PublicCutoverBootstrapError(
                "public cutover secret restore target is unsafe"
            )
        shutil.copytree(backup, restore, copy_function=shutil.copy2)
        _safe_secret_directory(
            restore,
            _OLD_SECRET_NAMES,
            expected_uid=self.expected_uid,
        )
        os.rename(restore, active)
        _fsync_directory(active.parent)

    def _rollback(
        self,
        *,
        source: Path,
        base: str,
        target: str,
        old_image: str | None,
        old_runtime_target: str | None,
        transition_attempted: bool,
    ) -> None:
        failures = 0
        if transition_attempted:
            try:
                self._stop_transition_redis(source)
            except (OSError, PublicCutoverBootstrapError):
                failures += 1
        if old_runtime_target is not None:
            try:
                self._restore_runtime(source, old_runtime_target)
            except (OSError, PublicCutoverBootstrapError):
                failures += 1
        try:
            self._restore_secrets(base=base, target=target)
        except (OSError, PublicCutoverBootstrapError):
            failures += 1
        if old_image is not None:
            try:
                self._command(
                    "/usr/bin/docker",
                    "image",
                    "tag",
                    old_image,
                    REDIS_IMAGE,
                )
            except PublicCutoverBootstrapError:
                failures += 1
        if failures:
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap rollback is incomplete"
            )

    def run(self) -> dict[str, object]:
        if not self.confirmed or len(_GENERATED_SECRET_NAMES) != 18:
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap is not explicitly confirmed"
            )
        _safe_directory(
            self.state_root,
            mode=0o700,
            expected_uid=self.expected_uid,
        )
        base, target = self._identity()
        if self.state_file.exists():
            existing_state = _read_json(self.state_file)
            if (
                existing_state.get("schema_version") == 1
                and existing_state.get("status") == "ready"
                and existing_state.get("base_commit") == base
                and existing_state.get("target_commit") == target
            ):
                return existing_state
            raise PublicCutoverBootstrapError(
                "public cutover bootstrap state is not reusable"
            )

        source = self._create_source(target)
        old_image: str | None = None
        old_runtime_target: str | None = None
        transition_attempted = False
        try:
            old_image, new_image = self._build_redis(source, target)
            backup, old_secrets = self._prepare_secrets(base=base, target=target)
            old_runtime_target = self._runtime_target(source)
            self._prepare_runtime(source)
            transition_attempted = True
            self._start_transition_redis(source)
            state: dict[str, object] = {
                "schema_version": 1,
                "status": "ready",
                "base_commit": base,
                "target_commit": target,
                "redis_image_id": new_image,
                "old_redis_image_id": old_image,
                "backup_dir": str(backup),
                "old_secrets_dir": str(old_secrets),
                "old_runtime_target": old_runtime_target,
            }
            _atomic_json(self.state_file, state)
            return state
        except (OSError, PublicCutoverBootstrapError, ValueError) as error:
            try:
                self._rollback(
                    source=source,
                    base=base,
                    target=target,
                    old_image=old_image,
                    old_runtime_target=old_runtime_target,
                    transition_attempted=transition_attempted,
                )
            except PublicCutoverBootstrapError as rollback_error:
                raise rollback_error from error
            raise
        finally:
            if source.exists():
                self._remove_source(source)


def main() -> int:
    try:
        state = PublicCutoverBootstrap(
            root=Path(os.environ.get("SMS_PLATFORM_ROOT", str(DEFAULT_ROOT))),
            runtime_root=Path(
                os.environ.get("SMS_RUNTIME_ROOT", str(DEFAULT_RUNTIME_ROOT))
            ),
            vendor_root=Path(
                os.environ.get("SMS_VENDOR_CREDENTIAL_ROOT", str(DEFAULT_VENDOR_ROOT))
            ),
            confirmed=os.environ.get("SMS_PUBLIC_CUTOVER_CONFIRMED") == "1",
        ).run()
    except (OSError, PublicCutoverBootstrapError, ValueError):
        print("public-cutover-bootstrap: blocked", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema_version": state["schema_version"],
                "status": state["status"],
                "base_commit": state["base_commit"],
                "target_commit": state["target_commit"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
