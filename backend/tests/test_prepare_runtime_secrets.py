from __future__ import annotations

import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "prepare_runtime_secrets.py"
SECRET_NAMES = frozenset(
    {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "api_key_pepper_key",
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
PKCS8_PEM_BEGIN = b"-----" + b"BEGIN " + b"PRIVATE " + b"KEY-----\n"
PKCS8_PEM_END = b"-----" + b"END " + b"PRIVATE " + b"KEY-----\n"


def load_module() -> ModuleType:
    if not SCRIPT.is_file():
        pytest.skip("runtime secret preprocessor is not implemented")
    spec = importlib.util.spec_from_file_location("prepare_runtime_secrets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module() -> ModuleType:
    return load_module()


def test_runtime_secret_preprocessor_exists() -> None:
    assert SCRIPT.is_file(), "runtime secret preprocessor is not implemented"


def make_source(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    source = tmp_path / "canonical"
    source.mkdir(mode=0o700)
    values = {
        name: f"fixture-value-for-{name}-never-log-this".encode() for name in SECRET_NAMES
    }
    private = X25519PrivateKey.from_private_bytes(b"p" * 32)
    values["audit_context_key"] = base64.b64encode(b"a" * 32)
    values["audit_system_api_context_key"] = base64.b64encode(b"i" * 32)
    values["audit_system_realtime_context_key"] = base64.b64encode(b"r" * 32)
    values["audit_system_bulk_context_key"] = base64.b64encode(b"b" * 32)
    values["alert_credential_private_key"] = base64.b64encode(b"p" * 32)
    values["alert_credential_public_key"] = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    values["redis_tls_server_key"] = (
        PKCS8_PEM_BEGIN
        + b"ZmFrZS10ZXN0LWtleS1uZXZlci11c2UtaW4tcHJvZHVjdGlvbg==\n"
        + PKCS8_PEM_END
    )
    for name, value in values.items():
        path = source / name
        path.write_bytes(value)
        path.chmod(0o600)
    tls_root = tmp_path / "redis-tls"
    tls_root.mkdir(mode=0o755, exist_ok=True)
    ca, certificate = redis_tls_public_paths(source)
    ca.write_bytes(b"fixture Redis CA certificate\n")
    certificate.write_bytes(b"fixture Redis three-SAN server certificate\n")
    ca.chmod(0o644)
    certificate.chmod(0o644)
    return source, values


def redis_tls_public_paths(source: Path) -> tuple[Path, Path]:
    tls_root = source.parent / "redis-tls"
    return tls_root / "ca.pem", tls_root / "server.pem"


def prepare_portably(module: ModuleType, source: Path, runtime: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    ca, certificate = redis_tls_public_paths(source)
    module.prepare(
        source_dir=source,
        runtime_root=runtime,
        mode="production",
        backend_owner=(uid, gid),
        postgres_owner=(uid, gid),
        migrate_owner=(uid, gid),
        require_root=False,
        redis_tls_ca_path=ca,
        redis_tls_certificate_path=certificate,
        redis_tls_public_owner=(uid, gid),
        production_source_owner=(uid, gid),
    )


def revoke_portably(module: ModuleType, source: Path, runtime: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    module.revoke_vendor(
        source_dir=source,
        runtime_root=runtime,
        mode="development",
        backend_owner=(uid, gid),
        postgres_owner=(uid, gid),
        migrate_owner=(uid, gid),
        require_root=False,
    )


def test_production_source_requires_the_fixed_directory_and_file_owner(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    uid, gid = os.getuid(), os.getgid()

    with pytest.raises(
        module.RuntimeSecretsError,
        match="production source directory owner must be root:root",
    ):
        module._validate_source_inventory(
            source,
            "production",
            production_owner=(uid + 1, gid),
        )

    with pytest.raises(
        module.RuntimeSecretsError,
        match="owner must match the production contract",
    ):
        module._read_source_file(
            source / "jwt_secret",
            "jwt_secret",
            expected_owner=(uid + 1, gid),
        )


def test_prepare_creates_service_specific_0400_copies(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)

    current = (runtime / "current").resolve(strict=True)
    assert {path.name for path in (current / "backend").iterdir()} == {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "api_key_pepper_key",
        "api_key_legacy_hmac_pepper",
        "audit_context_key",
        "audit_system_api_context_key",
        "audit_system_realtime_context_key",
        "audit_system_bulk_context_key",
        "alert_credential_public_key",
        "alert_credential_private_key",
        "jwt_secret",
        "ldap_bind_password",
        "metrics_scrape_token",
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
    }
    assert {path.name for path in (current / "postgres").iterdir()} == {
        "db_owner_password",
        "db_auth_password",
        "db_accept_password",
        "db_send_password",
        "db_callback_password",
        "db_export_password",
        "db_scheduler_password",
        "db_metrics_password",
    }
    assert {path.name for path in (current / "migrate").iterdir()} == {
        "db_owner_password",
        "audit_context_key",
        "audit_system_api_context_key",
        "audit_system_realtime_context_key",
        "audit_system_bulk_context_key",
    }
    assert {path.name for path in (current / "redis").iterdir()} == {
        "redis_broker_password",
        "redis_auth_password",
        "redis_control_password",
        "redis_tls_server_key",
    }
    assert not (current / "backend" / "redis_tls_server_key").exists()
    assert (current / "backend" / "api_key_legacy_hmac_pepper").read_bytes() == (
        module.LEGACY_API_KEY_PEPPER_TOMBSTONE
    )
    assert not (current / "postgres" / "api_key_legacy_hmac_pepper").exists()
    assert not (current / "migrate" / "api_key_legacy_hmac_pepper").exists()
    assert not (current / "redis" / "api_key_legacy_hmac_pepper").exists()
    files = [path for path in current.rglob("*") if path.is_file()]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in files)
    expected_owner = (os.getuid(), os.getgid())
    assert all((path.stat().st_uid, path.stat().st_gid) == expected_owner for path in files)
    assert not any(path.is_symlink() for path in files)


def test_prepare_copies_legacy_api_key_pepper_to_backend_only(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    legacy = source / "api_key_legacy_hmac_pepper"
    legacy.write_bytes(b"old-data-hmac-key-raw-text-never-log")
    legacy.chmod(0o600)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)

    current = (runtime / "current").resolve(strict=True)
    copied = (current / "backend" / "api_key_legacy_hmac_pepper").read_bytes()
    assert copied == b"old-data-hmac-key-raw-text-never-log"
    assert not (current / "postgres" / "api_key_legacy_hmac_pepper").exists()
    assert not (current / "migrate" / "api_key_legacy_hmac_pepper").exists()
    assert not (current / "redis" / "api_key_legacy_hmac_pepper").exists()


def test_production_generation_binds_only_public_redis_tls_fingerprints(
    module: ModuleType, tmp_path: Path
) -> None:
    source, values = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    current = (runtime / "current").resolve(strict=True)
    metadata_path = current / module.REDIS_TLS_PUBLIC_METADATA_NAME
    ca, certificate = redis_tls_public_paths(source)

    metadata = json.loads(metadata_path.read_text(encoding="ascii"))

    assert metadata == {
        "schema_version": 1,
        "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(),
        "server_certificate_sha256": hashlib.sha256(
            certificate.read_bytes()
        ).hexdigest(),
    }
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o400
    serialized = metadata_path.read_bytes()
    assert values["redis_tls_server_key"] not in serialized
    assert hashlib.sha256(values["redis_tls_server_key"]).hexdigest().encode() not in serialized


def test_prepare_rejects_symlinked_source_directory(module: ModuleType, tmp_path: Path) -> None:
    source, _ = make_source(tmp_path)
    linked_source = tmp_path / "linked-canonical"
    linked_source.symlink_to(source, target_is_directory=True)

    with pytest.raises(module.RuntimeSecretsError, match="source directory"):
        prepare_portably(module, linked_source, tmp_path / "runtime")


@pytest.mark.parametrize("defect", ["missing", "extra", "empty", "symlink"])
def test_prepare_rejects_invalid_source_inventory(
    module: ModuleType, tmp_path: Path, defect: str
) -> None:
    source, _ = make_source(tmp_path)
    target = source / "vendor_secret_key"
    if defect == "missing":
        target.unlink()
    elif defect == "extra":
        extra = source / "unexpected-secret"
        extra.write_bytes(b"not-a-secret")
        extra.chmod(0o600)
    elif defect == "empty":
        target.write_bytes(b"")
        target.chmod(0o600)
    else:
        target.unlink()
        target.symlink_to(source / "vendor_secret_name")

    with pytest.raises(module.RuntimeSecretsError):
        prepare_portably(module, source, tmp_path / "runtime")


@pytest.mark.parametrize("target", ["directory", "file"])
def test_prepare_rejects_imprecise_source_modes(
    module: ModuleType, tmp_path: Path, target: str
) -> None:
    source, _ = make_source(tmp_path)
    if target == "directory":
        source.chmod(0o750)
    else:
        (source / "jwt_secret").chmod(0o640)

    with pytest.raises(module.RuntimeSecretsError, match="mode"):
        prepare_portably(module, source, tmp_path / "runtime")


def test_prepare_rejects_reused_audit_keys_across_producer_domains(
    module: ModuleType, tmp_path: Path
) -> None:
    source, values = make_source(tmp_path)
    (source / "audit_system_bulk_context_key").write_bytes(
        values["audit_system_realtime_context_key"]
    )
    (source / "audit_system_bulk_context_key").chmod(0o600)

    with pytest.raises(module.RuntimeSecretsError, match="pairwise independent"):
        prepare_portably(module, source, tmp_path / "runtime")


@pytest.mark.parametrize(
    ("first", "second", "second_suffix", "message"),
    (
        (
            "redis_broker_password",
            "redis_auth_password",
            b"\r\n",
            "Redis ACL passwords",
        ),
        (
            "db_owner_password",
            "db_metrics_password",
            b"\n",
            "database role passwords",
        ),
    ),
)
def test_production_rejects_effectively_reused_service_passwords_before_generation(
    module: ModuleType,
    tmp_path: Path,
    first: str,
    second: str,
    second_suffix: bytes,
    message: str,
) -> None:
    source, values = make_source(tmp_path)
    (source / second).write_bytes(values[first] + second_suffix)
    (source / second).chmod(0o600)
    runtime = tmp_path / "runtime"

    with pytest.raises(module.RuntimeSecretsError, match=message):
        prepare_portably(module, source, runtime)

    assert not (runtime / "current").exists()
    assert not (runtime / "generations").exists()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        ("redis_broker_password", b"A" * 31, "Redis ACL passwords"),
        (
            "redis_auth_password",
            b"A" * 40 + b"\nuser default on nopass ~* &* +@all",
            "Redis ACL passwords",
        ),
        ("db_owner_password", b"B" * 40 + b"\n", "database role passwords"),
        ("db_send_password", b"unsafe password value" * 3, "database role passwords"),
    ),
)
def test_production_rejects_service_passwords_outside_single_line_contract(
    module: ModuleType,
    tmp_path: Path,
    name: str,
    value: bytes,
    message: str,
) -> None:
    source, _ = make_source(tmp_path)
    target = source / name
    target.write_bytes(value)
    target.chmod(0o600)

    with pytest.raises(module.RuntimeSecretsError, match=message):
        prepare_portably(module, source, tmp_path / "runtime")


def test_production_rejects_non_pem_redis_tls_server_key(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    key = source / "redis_tls_server_key"
    key.write_bytes(b"not-a-private-key")
    key.chmod(0o600)

    with pytest.raises(module.RuntimeSecretsError, match="PKCS#8 PEM"):
        prepare_portably(module, source, tmp_path / "runtime")


@pytest.mark.parametrize(
    ("target_name", "defect"),
    (
        ("ca.pem", "missing"),
        ("ca.pem", "mode"),
        ("server.pem", "symlink"),
    ),
)
def test_production_rejects_unsafe_redis_tls_public_material_before_generation(
    module: ModuleType,
    tmp_path: Path,
    target_name: str,
    defect: str,
) -> None:
    source, _ = make_source(tmp_path)
    target = source.parent / "redis-tls" / target_name
    if defect == "missing":
        target.unlink()
    elif defect == "mode":
        target.chmod(0o600)
    else:
        other = source.parent / "redis-tls" / "ca.pem"
        target.unlink()
        target.symlink_to(other)
    runtime = tmp_path / "runtime"

    with pytest.raises(module.RuntimeSecretsError, match="Redis TLS"):
        prepare_portably(module, source, runtime)

    assert not (runtime / "current").exists()
    assert not (runtime / "generations").exists()


def test_development_prepare_does_not_require_redis_tls_public_material(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    for path in redis_tls_public_paths(source):
        path.unlink()
    uid, gid = os.getuid(), os.getgid()

    module.prepare(
        source_dir=source,
        runtime_root=tmp_path / "development-runtime",
        mode="development",
        backend_owner=(uid, gid),
        postgres_owner=(uid, gid),
        migrate_owner=(uid, gid),
        redis_owner=(uid, gid),
        require_root=False,
    )

    current = (tmp_path / "development-runtime" / "current").resolve(strict=True)
    assert not (current / module.REDIS_TLS_PUBLIC_METADATA_NAME).exists()


def verify_redis_tls_rotation_portably(
    module: ModuleType,
    *,
    source: Path,
    runtime: Path,
    baseline_target: str,
) -> None:
    uid, gid = os.getuid(), os.getgid()
    ca, certificate = redis_tls_public_paths(source)
    module.verify_ordinary_redis_tls_rotation(
        source_dir=source,
        runtime_root=runtime,
        baseline_target=baseline_target,
        ca_path=ca,
        certificate_path=certificate,
        expected_redis_owner=(uid, gid),
        expected_metadata_owner=(uid, gid),
        require_root=False,
    )


def test_ordinary_redis_tls_rotation_accepts_only_unchanged_complete_tuple(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    baseline_target = os.readlink(runtime / "current")
    prepare_portably(module, source, runtime)

    verify_redis_tls_rotation_portably(
        module,
        source=source,
        runtime=runtime,
        baseline_target=baseline_target,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "host-ca",
        "host-certificate",
        "source-key",
        "baseline-metadata-missing",
        "current-metadata-missing",
        "baseline-key-changed",
    ),
)
def test_ordinary_redis_tls_rotation_requires_full_stop_for_any_tuple_change(
    module: ModuleType,
    tmp_path: Path,
    mutation: str,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    baseline_target = os.readlink(runtime / "current")
    prepare_portably(module, source, runtime)
    current_target = os.readlink(runtime / "current")
    ca, certificate = redis_tls_public_paths(source)
    if mutation == "host-ca":
        ca.write_bytes(b"replacement Redis CA certificate\n")
    elif mutation == "host-certificate":
        certificate.write_bytes(b"replacement Redis server certificate\n")
    elif mutation == "source-key":
        key = source / "redis_tls_server_key"
        key.write_bytes(
            PKCS8_PEM_BEGIN
            + b"cmVwbGFjZW1lbnQtdGVzdC1rZXktbmV2ZXItdXNlLWluLXByb2Q=\n"
            + PKCS8_PEM_END
        )
        key.chmod(0o600)
    elif mutation.endswith("metadata-missing"):
        target = baseline_target if mutation.startswith("baseline") else current_target
        (runtime / target / module.REDIS_TLS_PUBLIC_METADATA_NAME).unlink()
    else:
        key = runtime / baseline_target / "redis" / "redis_tls_server_key"
        key.chmod(0o600)
        key.write_bytes(
            PKCS8_PEM_BEGIN
            + b"dGFtcGVyZWQtcnVudGltZS1rZXktbmV2ZXItdXNlLWluLXByb2Q=\n"
            + PKCS8_PEM_END
        )
        key.chmod(0o400)

    with pytest.raises(module.RuntimeSecretsError, match="full-stop three-domain"):
        verify_redis_tls_rotation_portably(
            module,
            source=source,
            runtime=runtime,
            baseline_target=baseline_target,
        )


def test_production_rejects_but_development_ignores_dev_apikeys(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    dev_keys = source / "dev-apikeys.txt"
    dev_keys.write_bytes(b"fixture-development-key")
    dev_keys.chmod(0o600)

    with pytest.raises(module.RuntimeSecretsError, match="inventory"):
        prepare_portably(module, source, tmp_path / "production-runtime")

    uid, gid = os.getuid(), os.getgid()
    module.prepare(
        source_dir=source,
        runtime_root=tmp_path / "development-runtime",
        mode="development",
        backend_owner=(uid, gid),
        postgres_owner=(uid, gid),
        migrate_owner=(uid, gid),
        require_root=False,
    )
    current = (tmp_path / "development-runtime" / "current").resolve(strict=True)
    assert not any(path.name == "dev-apikeys.txt" for path in current.rglob("*"))


def test_prepare_rejects_oversized_source_file(module: ModuleType, tmp_path: Path) -> None:
    source, _ = make_source(tmp_path)
    oversized = source / "jwt_secret"
    oversized.write_bytes(b"x" * (module.MAX_SECRET_BYTES + 1))
    oversized.chmod(0o600)

    with pytest.raises(module.RuntimeSecretsError, match="policy"):
        prepare_portably(module, source, tmp_path / "runtime")


def test_production_cli_rejects_non_root_execution(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _ = make_source(tmp_path)
    monkeypatch.setattr(module.os, "geteuid", lambda: 501)

    result = module.main(
        [
            "prepare",
            "--source-dir",
            str(source),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--mode",
            "production",
        ]
    )

    assert result == 1
    assert "root" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("operation", ["fchown", "fsync", "replace"])
def test_prepare_failure_does_not_change_old_current_target(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    old_target = os.readlink(runtime / "current")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected operation failure")

    monkeypatch.setattr(module.os, operation, fail)
    with pytest.raises(module.RuntimeSecretsError):
        prepare_portably(module, source, runtime)

    assert os.readlink(runtime / "current") == old_target
    assert (runtime / old_target).is_dir()


def test_prepare_rejects_concurrent_nonblocking_lock(module: ModuleType, tmp_path: Path) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    lock_fd = os.open(runtime / "prepare.lock", os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.RuntimeSecretsError, match="lock"):
            prepare_portably(module, source, runtime)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_prepare_does_not_validate_source_until_runtime_lock_is_held(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    lock_fd = os.open(runtime / "prepare.lock", os.O_RDWR)

    def unexpected_validation(*_args: object, **_kwargs: object) -> dict[str, bytes]:
        raise AssertionError("source validation ran outside the runtime lock")

    monkeypatch.setattr(module, "_validate_source", unexpected_validation)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.RuntimeSecretsError, match="lock"):
            prepare_portably(module, source, runtime)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_current_target_and_activate_switch_only_valid_generation_metadata(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    old_target = os.readlink(runtime / "current")
    prepare_portably(module, source, runtime)
    new_target = os.readlink(runtime / "current")

    assert module.current_target(runtime_root=runtime) == new_target
    module.activate(runtime_root=runtime, target=old_target)

    assert os.readlink(runtime / "current") == old_target
    assert module.current_target(runtime_root=runtime) == old_target


@pytest.mark.parametrize(
    "target",
    [
        "generations/..",
        "generations/.",
        ".",
        "generations/not-a-generation",
        "generations/generation-deadbeef/child",
    ],
)
def test_current_target_rejects_noncanonical_or_escaping_target(
    module: ModuleType, tmp_path: Path, target: str
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    generations = runtime / "generations"
    generations.mkdir(mode=0o700)
    (generations / "not-a-generation").mkdir(mode=0o700)
    (runtime / "current").symlink_to(target)

    with pytest.raises(module.RuntimeSecretsError, match="generation"):
        module.current_target(runtime_root=runtime)


def test_activate_rejects_invalid_target_without_changing_current(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    old_target = os.readlink(runtime / "current")

    with pytest.raises(module.RuntimeSecretsError, match="generation"):
        module.activate(runtime_root=runtime, target="generations/..")

    assert os.readlink(runtime / "current") == old_target


def test_activate_refuses_to_overwrite_invalid_current_pointer(
    module: ModuleType, tmp_path: Path
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    target = os.readlink(runtime / "current")
    (runtime / "current").unlink()
    (runtime / "current").write_text("invalid pointer", encoding="utf-8")

    with pytest.raises(module.RuntimeSecretsError, match="current"):
        module.activate(runtime_root=runtime, target=target)

    assert (runtime / "current").read_text(encoding="utf-8") == "invalid pointer"


def test_current_target_and_activate_cli_use_only_generation_metadata(
    module: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    target = os.readlink(runtime / "current")

    assert module.main(["current-target", "--runtime-root", str(runtime)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == target
    assert captured.err == ""

    assert (
        module.main(
            ["activate", "--runtime-root", str(runtime), "--target", target]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "runtime secrets activated"


def test_cleanup_stale_retains_only_current_generation(module: ModuleType, tmp_path: Path) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    prepare_portably(module, source, runtime)

    module.cleanup(runtime_root=runtime, remove_all=False)

    current = (runtime / "current").resolve(strict=True)
    generations = [path for path in (runtime / "generations").iterdir() if path.is_dir()]
    assert generations == [current]


def test_cleanup_all_stays_inside_runtime_root(module: ModuleType, tmp_path: Path) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside-sentinel"
    outside.write_text("keep", encoding="utf-8")
    prepare_portably(module, source, runtime)

    module.cleanup(runtime_root=runtime, remove_all=True)

    assert outside.read_text(encoding="utf-8") == "keep"
    assert not (runtime / "current").exists()
    assert not any((runtime / "generations").iterdir())


def test_cleanup_all_unlinks_current_before_delete_failure_and_prepare_recovers(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    prepare_portably(module, source, runtime)
    real_rmtree = module.shutil.rmtree
    calls = 0

    def fail_first_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected cleanup failure")
        real_rmtree(path)

    monkeypatch.setattr(module.shutil, "rmtree", fail_first_rmtree)
    with pytest.raises(module.RuntimeSecretsError, match="cleanup"):
        module.cleanup(runtime_root=runtime, remove_all=True)

    assert not os.path.lexists(runtime / "current")

    monkeypatch.setattr(module.shutil, "rmtree", real_rmtree)
    prepare_portably(module, source, runtime)
    assert (runtime / "current").resolve(strict=True).is_dir()


def test_cleanup_all_refuses_while_lock_is_held(module: ModuleType, tmp_path: Path) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    lock_fd = os.open(runtime / "prepare.lock", os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.RuntimeSecretsError, match="lock"):
            module.cleanup(runtime_root=runtime, remove_all=True)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert (runtime / "current").resolve(strict=True).is_dir()


def test_cli_output_contains_no_secret_value_length_or_hash(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, values = make_source(tmp_path)
    uid, gid = os.getuid(), os.getgid()
    monkeypatch.setattr(
        module,
        "_owners_for_cli",
        lambda _mode: ((uid, gid), (uid, gid), (uid, gid), (uid, gid), False),
    )
    result = module.main(
        [
            "prepare",
            "--source-dir",
            str(source),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--mode",
            "development",
        ]
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert result == 0
    for value in values.values():
        assert value.decode() not in output
        assert str(len(value)) not in output
        assert hashlib.sha256(value).hexdigest() not in output


def test_prepare_reads_both_vendor_values_from_one_active_credential_generation(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
    from vendor_credential_store import VendorCredentials, VendorCredentialStore

    source, values = make_source(tmp_path)
    credential_root = tmp_path / "vendor-credentials"
    store = VendorCredentialStore(credential_root)
    store.install(VendorCredentials("page-name-v1", "page-key-v1"))
    store.install(VendorCredentials("page-name-v2", "page-key-v2"))
    runtime = tmp_path / "runtime"
    uid, gid = os.getuid(), os.getgid()

    module.prepare(
        source_dir=source,
        runtime_root=runtime,
        mode="development",
        backend_owner=(uid, gid),
        postgres_owner=(uid, gid),
        migrate_owner=(uid, gid),
        require_root=False,
        vendor_credential_root=credential_root,
    )

    backend = (runtime / "current").resolve(strict=True) / "backend"
    assert (backend / "vendor_secret_name").read_text() == "page-name-v2"
    assert (backend / "vendor_secret_key").read_text() == "page-key-v2"
    assert (backend / "data_aes_key").read_bytes() == values["data_aes_key"]


def test_revoke_vendor_never_reads_canonical_vendor_values(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = make_source(tmp_path)
    observed: list[str] = []
    real_read = module._read_source_file

    def recording_read(path: Path, logical_name: str) -> bytes:
        observed.append(logical_name)
        if logical_name.startswith("vendor_"):
            raise AssertionError("vendor source content must not be read during revocation")
        return real_read(path, logical_name)

    monkeypatch.setattr(module, "_read_source_file", recording_read)

    revoke_portably(module, source, tmp_path / "runtime")

    assert set(observed) == SECRET_NAMES - {
        "vendor_secret_name",
        "vendor_secret_key",
    }


def test_revoke_vendor_materializes_only_tombstones_and_preserves_other_secrets(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    source, values = make_source(tmp_path)
    runtime = tmp_path / "runtime"

    revoke_portably(module, source, runtime)

    current = (runtime / "current").resolve(strict=True)
    backend = current / "backend"
    assert (backend / "vendor_secret_name").read_bytes() == (
        module.VENDOR_REVOCATION_TOMBSTONE
    )
    assert (backend / "vendor_secret_key").read_bytes() == (
        module.VENDOR_REVOCATION_TOMBSTONE
    )
    for name in SECRET_NAMES - {"vendor_secret_name", "vendor_secret_key"}:
        services = (
            ("migrate", "postgres")
            if name == "db_owner_password"
            else ("backend", "postgres")
            if name.startswith("db_")
            else ("backend",)
        )
        for service in services:
            if (current / service / name).exists():
                assert (current / service / name).read_bytes() == values[name]
    all_runtime_values = [path.read_bytes() for path in current.rglob("*") if path.is_file()]
    for name in ("vendor_secret_name", "vendor_secret_key"):
        assert values[name] not in all_runtime_values
        assert hashlib.sha256(values[name]).hexdigest().encode() not in all_runtime_values
    assert module.verify_vendor_revoked(runtime_root=runtime) is None


def test_verify_vendor_revoked_rejects_tampered_current_generation(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    revoke_portably(module, source, runtime)
    target = (runtime / "current").resolve(strict=True) / "backend" / "vendor_secret_key"
    target.chmod(0o600)
    target.write_bytes(b"tampered-not-a-key")
    target.chmod(0o400)

    with pytest.raises(module.RuntimeSecretsError, match="revoked"):
        module.verify_vendor_revoked(runtime_root=runtime)


def test_revoke_vendor_failure_preserves_previous_current_target(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    previous = os.readlink(runtime / "current")
    real_replace = module.os.replace

    def fail_current_switch(source_path: Path, target_path: Path) -> None:
        if Path(target_path) == runtime / "current":
            raise OSError("injected current switch failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr(module.os, "replace", fail_current_switch)

    with pytest.raises(module.RuntimeSecretsError):
        revoke_portably(module, source, runtime)

    assert os.readlink(runtime / "current") == previous
    assert (runtime / previous).is_dir()


def test_revoke_and_verify_cli_outputs_only_fixed_status(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, values = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    uid, gid = os.getuid(), os.getgid()
    monkeypatch.setattr(
        module,
        "_owners_for_cli",
        lambda _mode: ((uid, gid), (uid, gid), (uid, gid), (uid, gid), False),
    )

    assert (
        module.main(
            [
                "revoke-vendor",
                "--source-dir",
                str(source),
                "--runtime-root",
                str(runtime),
                "--mode",
                "development",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "runtime vendor credentials revoked\n"
    assert (
        module.main(["verify-vendor-revoked", "--runtime-root", str(runtime)])
        == 0
    )
    output = capsys.readouterr().out
    assert output == "runtime vendor credentials are revoked\n"
    assert "generation-" not in output
    for value in values.values():
        assert value.decode() not in output


def test_verify_only_current_generation_rejects_stale_runtime_copies(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    source, _ = make_source(tmp_path)
    runtime = tmp_path / "runtime"
    prepare_portably(module, source, runtime)
    revoke_portably(module, source, runtime)

    with pytest.raises(module.RuntimeSecretsError, match="stale"):
        module.verify_only_current_generation(runtime_root=runtime)

    module.cleanup(runtime_root=runtime, remove_all=False)
    assert module.verify_only_current_generation(runtime_root=runtime) is None
