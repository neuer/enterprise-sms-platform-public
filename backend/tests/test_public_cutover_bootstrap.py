from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))

import public_cutover_bootstrap as module  # noqa: E402


def _write(path: Path, value: str) -> None:
    path.write_text(f"{value}\n", encoding="ascii")
    path.chmod(0o600)


def _old_root(tmp_path: Path) -> Path:
    root = tmp_path / "platform"
    secrets = root / "deploy/secrets"
    secrets.mkdir(parents=True, mode=0o700)
    for name in sorted(module._OLD_SECRET_NAMES):
        _write(secrets / name, f"old-{name}")
    _write(secrets / module._DEV_SECRET, "development-api-key")
    root_env = root / ".env"
    _write(root_env, "ENVIRONMENT=development")
    return root


def _bootstrap(
    tmp_path: Path,
    *,
    secret_factory: Callable[[], str] | None = None,
) -> module.PublicCutoverBootstrap:
    root = _old_root(tmp_path)
    counter = iter(f"generated-{index:02d}-{'x' * 48}" for index in range(11))
    return module.PublicCutoverBootstrap(
        root=root,
        runtime_root=tmp_path / "runtime",
        vendor_root=tmp_path / "vendor",
        host_manifest=tmp_path / "manifest.json",
        state_root=tmp_path / "state",
        expected_uid=os.geteuid(),
        confirmed=True,
        secret_factory=secret_factory or (lambda: next(counter)),
    )


def test_secret_transition_generates_crypto_keys_and_keeps_backup(
    tmp_path: Path,
) -> None:
    bootstrap = _bootstrap(tmp_path)
    bootstrap.state_root.mkdir(mode=0o700)

    backup, old_secrets = bootstrap._prepare_secrets(
        base="a" * 40,
        target="b" * 40,
    )

    active = bootstrap.root / "deploy/secrets"
    assert {entry.name for entry in active.iterdir()} == (
        set(module._NEW_SECRET_NAMES) | {module._DEV_SECRET}
    )
    assert module._NEW_SECRET_NAMES - module._OLD_SECRET_NAMES == (
        module._GENERATED_SECRET_NAMES
    )
    assert len(module._GENERATED_SECRET_NAMES) == 17
    assert len((active / "audit_context_key").read_text().strip()) == 44
    for domain in ("api", "realtime", "bulk"):
        assert len(
            (active / f"audit_system_{domain}_context_key").read_text().strip()
        ) == 44
    assert len((active / "alert_credential_public_key").read_text().strip()) == 44
    assert len((active / "alert_credential_private_key").read_text().strip()) == 44
    assert "db_app_password" not in {entry.name for entry in active.iterdir()}
    assert all(
        entry.stat().st_mode & 0o777 == 0o600
        for entry in active.iterdir()
    )
    assert old_secrets.is_dir()
    assert (old_secrets / "db_app_password").read_text(encoding="ascii") == (
        "old-db_app_password\n"
    )
    assert (backup / "secrets/db_app_password").is_file()
    assert (backup / "root.env").read_text(encoding="ascii") == (
        "ENVIRONMENT=development\n"
    )


def test_secret_transition_normalizes_directory_in_setgid_root(
    tmp_path: Path,
) -> None:
    bootstrap = _bootstrap(tmp_path)
    bootstrap.root.chmod(0o2755)
    bootstrap.state_root.mkdir(mode=0o700)

    bootstrap._prepare_secrets(base="a" * 40, target="b" * 40)

    assert bootstrap.root.joinpath("deploy/secrets").stat().st_mode & 0o7777 == 0o700


def test_existing_redis_image_is_bound_to_one_healthy_compose_container(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(
            self,
            argv: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
        ) -> str:
            call = tuple(argv)
            calls.append(call)
            if call[1] == "ps":
                return "abcdef123456"
            if call[-2:] == ("{{.Image}}", "abcdef123456"):
                return f"sha256:{'1' * 64}"
            if call[-1] == "abcdef123456":
                return "sms-platform|redis|running|healthy"
            raise AssertionError(call)

    bootstrap = module.PublicCutoverBootstrap(
        root=tmp_path,
        expected_uid=os.geteuid(),
        confirmed=True,
        runner=Runner(),
    )

    assert bootstrap._running_redis_image() == f"sha256:{'1' * 64}"
    assert calls[0][1:3] == ("ps", "--filter")
    assert "{{.Image}}" in calls[-1]


def test_redis_build_failure_restores_existing_image_tag(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    old_image = f"sha256:{'1' * 64}"

    class Runner:
        def run(
            self,
            argv: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
        ) -> str:
            call = tuple(argv)
            calls.append(call)
            if call[:2] == ("/usr/bin/python3", "-c"):
                return "0039_context_encryption_v2"
            if call[1] == "build":
                raise module.PublicCutoverBootstrapError("build failed")
            if call[1:3] == ("image", "tag"):
                return ""
            raise AssertionError(call)

    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text("1.6.0\n", encoding="utf-8")
    bootstrap = module.PublicCutoverBootstrap(
        root=tmp_path,
        expected_uid=os.geteuid(),
        confirmed=True,
        runner=Runner(),
    )
    bootstrap._running_redis_image = lambda: old_image  # type: ignore[method-assign]

    with pytest.raises(module.PublicCutoverBootstrapError, match="build failed"):
        bootstrap._build_redis(source, "b" * 40)

    assert calls[-1] == (
        "/usr/bin/docker",
        "image",
        "tag",
        old_image,
        module.REDIS_IMAGE,
    )


def test_failed_transition_restores_old_authoritative_secrets(
    tmp_path: Path,
) -> None:
    class FailingBootstrap(module.PublicCutoverBootstrap):
        def _identity(self) -> tuple[str, str]:
            return "a" * 40, "b" * 40

        def _create_source(self, target: str) -> Path:
            source = self.state_root / f"source-{target}"
            source.mkdir()
            return source

        def _remove_source(self, source: Path) -> None:
            shutil.rmtree(source)

        def _build_redis(self, source: Path, target: str) -> tuple[str, str]:
            return f"sha256:{'1' * 64}", f"sha256:{'2' * 64}"

        def _runtime_target(self, source: Path) -> str:
            return f"generations/generation-{'3' * 32}"

        def _prepare_runtime(self, source: Path) -> None:
            return None

        def _start_transition_redis(self, source: Path) -> None:
            raise module.PublicCutoverBootstrapError("synthetic failure")

        def _stop_transition_redis(self, source: Path) -> None:
            return None

        def _restore_runtime(self, source: Path, target: str) -> None:
            return None

        def _command(
            self,
            *argv: str,
            environment: Mapping[str, str] | None = None,
        ) -> str:
            return ""

    prepared = _bootstrap(tmp_path)
    bootstrap = FailingBootstrap(
        root=prepared.root,
        runtime_root=prepared.runtime_root,
        vendor_root=prepared.vendor_root,
        host_manifest=prepared.host_manifest,
        state_root=prepared.state_root,
        expected_uid=os.geteuid(),
        confirmed=True,
        secret_factory=prepared.secret_factory,
    )

    with pytest.raises(module.PublicCutoverBootstrapError, match="synthetic"):
        bootstrap.run()

    active = bootstrap.root / "deploy/secrets"
    assert {entry.name for entry in active.iterdir()} == (
        set(module._OLD_SECRET_NAMES) | {module._DEV_SECRET}
    )
    assert (active / "db_app_password").read_text(encoding="ascii") == (
        "old-db_app_password\n"
    )
    assert list(bootstrap.state_root.glob("failed-secrets-*"))
    assert list(bootstrap.state_root.glob("backup-*"))
    assert not bootstrap.state_file.exists()


def test_successful_transition_records_only_safe_reusable_state(
    tmp_path: Path,
) -> None:
    class SuccessfulBootstrap(module.PublicCutoverBootstrap):
        def _identity(self) -> tuple[str, str]:
            return "a" * 40, "b" * 40

        def _create_source(self, target: str) -> Path:
            source = self.state_root / f"source-{target}"
            source.mkdir()
            return source

        def _remove_source(self, source: Path) -> None:
            shutil.rmtree(source)

        def _build_redis(self, source: Path, target: str) -> tuple[str, str]:
            return f"sha256:{'1' * 64}", f"sha256:{'2' * 64}"

        def _runtime_target(self, source: Path) -> str:
            return f"generations/generation-{'3' * 32}"

        def _prepare_runtime(self, source: Path) -> None:
            return None

        def _start_transition_redis(self, source: Path) -> None:
            return None

    prepared = _bootstrap(tmp_path)
    bootstrap = SuccessfulBootstrap(
        root=prepared.root,
        runtime_root=prepared.runtime_root,
        vendor_root=prepared.vendor_root,
        host_manifest=prepared.host_manifest,
        state_root=prepared.state_root,
        expected_uid=os.geteuid(),
        confirmed=True,
        secret_factory=prepared.secret_factory,
    )

    state = bootstrap.run()

    assert state["status"] == "ready"
    assert bootstrap.state_file.stat().st_mode & 0o777 == 0o600
    assert bootstrap.run() == state
    serialized = bootstrap.state_file.read_text(encoding="utf-8")
    assert "generated-" not in serialized
    assert "old-db_" not in serialized


def test_bootstrap_rejects_missing_explicit_confirmation(tmp_path: Path) -> None:
    bootstrap = _bootstrap(tmp_path)
    bootstrap.confirmed = False

    with pytest.raises(
        module.PublicCutoverBootstrapError,
        match="explicitly confirmed",
    ):
        bootstrap.run()

    assert not bootstrap.state_root.exists()
