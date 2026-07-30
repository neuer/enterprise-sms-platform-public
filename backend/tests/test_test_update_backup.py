from __future__ import annotations

import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import test_update_backup as backup_module  # noqa: E402

BackupConfig = backup_module.BackupConfig
BackupService = backup_module.TestUpdateBackup
BackupError = backup_module.TestUpdateBackupError
require_inherited_lifecycle_lock = backup_module.require_inherited_lifecycle_lock


class FakeRunner:
    def __init__(self, *, restore_fails: bool = False) -> None:
        self.restore_fails = restore_fails
        self.calls: list[tuple[list[str], bytes | None]] = []

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        self.calls.append((list(command), input_bytes))
        if command[0] == "pg_dump":
            return b"PGDMP\x01private-test-data"
        if command[0] == "pg_restore":
            if self.restore_fails:
                raise RuntimeError("restore list failed")
            assert input_bytes == b"PGDMP\x01private-test-data"
            return b"TABLE public sms_batch\n"
        raise AssertionError(f"unexpected command: {command}")


def _config(tmp_path: Path) -> BackupConfig:
    key = tmp_path / "checkpoint.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    output = tmp_path / "checkpoints"
    return BackupConfig(
        output_root=output,
        key_file=key,
        database="sms_platform",
        pg_dump_argv=("pg_dump", "--format=custom", "sms_platform"),
        pg_restore_argv=("pg_restore", "--list", "-"),
        runtime_root=tmp_path / "runtime",
    )


def _clock() -> datetime:
    return datetime(2026, 7, 16, 8, 30, tzinfo=UTC)


def test_checkpoint_encrypts_before_disk_and_marks_complete_after_restore_readability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_checks: list[Path] = []
    monkeypatch.setattr(
        backup_module,
        "require_inherited_lifecycle_lock",
        lambda root: lock_checks.append(root),
    )
    config = _config(tmp_path)
    runner = FakeRunner()

    result = BackupService(runner, clock=_clock).create(config, "update-1")

    assert lock_checks == [config.runtime_root]
    assert result.complete is True
    assert stat.S_IMODE(result.ciphertext_file.stat().st_mode) == 0o600
    ciphertext = result.ciphertext_file.read_bytes()
    assert b"private-test-data" not in ciphertext
    assert not list(config.output_root.rglob("*.dump"))
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["restore_readable"] is True
    rendered = json.dumps(manifest).casefold()
    assert "key_file" not in rendered
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "sha256" not in rendered
    nonce = bytes.fromhex(manifest["nonce"])
    assert AESGCM(b"k" * 32).decrypt(nonce, ciphertext, b"sms-test-update-v1") == (
        b"PGDMP\x01private-test-data"
    )
    assert [call[0][0] for call in runner.calls] == ["pg_dump", "pg_restore"]


def test_restore_readability_failure_never_publishes_complete_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_module, "require_inherited_lifecycle_lock", lambda _root: None)
    config = _config(tmp_path)

    with pytest.raises(RuntimeError, match="restore list failed"):
        BackupService(FakeRunner(restore_fails=True), clock=_clock).create(
            config,
            "update-1",
        )

    assert not config.output_root.joinpath("update-1").exists()
    assert not list(config.output_root.rglob("manifest.json"))


def test_backup_rejects_missing_inherited_lifecycle_lock(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="lifecycle lock"):
        require_inherited_lifecycle_lock(tmp_path / "runtime", environment={})


@pytest.mark.parametrize("mode", [0o644, 0o4000])
def test_backup_rejects_unsafe_independent_key_file(tmp_path: Path, mode: int) -> None:
    config = _config(tmp_path)
    config.key_file.chmod(mode)

    with pytest.raises(BackupError, match="checkpoint key file is unsafe"):
        BackupService(FakeRunner(), clock=_clock).create(
            config,
            "update-1",
            verify_lock=False,
        )
