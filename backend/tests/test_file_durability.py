from __future__ import annotations

import base64
import os
import stat
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from app.services.crypto import CryptoService
from app.services.export_file import ExportFileCodec, ExportWriteInterrupted
from app.services.file_durability import fsync_directory
from app.services.import_file import ImportFileCodec

LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")


def export_crypto() -> CryptoService:
    key = base64.b64encode(b"e" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def import_crypto() -> CryptoService:
    key = base64.b64encode(b"k" * 32).decode("ascii")
    return CryptoService.from_secret_values(key, key)


async def rows() -> AsyncIterator[tuple[object, ...]]:
    yield ("13800138000", "系统通知")


def crash_at(stage: str):
    def hook(name: str) -> None:
        if name == stage:
            raise ExportWriteInterrupted(name)

    return hook


@pytest.mark.asyncio
async def test_import_and_export_share_directory_fsync_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export_root = tmp_path / "exports"
    import_root = tmp_path / "imports"
    fsynced: list[Path] = []
    real = fsync_directory

    def tracking(path: Path) -> None:
        fsynced.append(path.resolve())
        real(path)

    monkeypatch.setattr(
        "app.services.export_file.fsync_directory",
        tracking,
    )
    monkeypatch.setattr(
        "app.services.import_file.fsync_directory",
        tracking,
    )

    export_codec = ExportFileCodec(export_crypto(), export_root)
    import_codec = ImportFileCodec(import_crypto(), import_root)
    await export_codec.write_csv(9, LEASE_ID, ("phone", "content"), rows())
    import_id = UUID("11111111-1111-4111-8111-111111111111")
    import_codec.stage(import_id, BytesIO(b"13800138000\n"), size=12, max_bytes=12)

    assert export_root.resolve() in fsynced
    assert import_root.resolve() in fsynced


@pytest.mark.asyncio
async def test_export_write_fsyncs_file_then_renames_then_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace
    real_chmod = os.chmod

    def tracking_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("fsync:dir" if stat.S_ISDIR(mode) else "fsync:file")
        real_fsync(descriptor)

    def tracking_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        events.append("replace")
        real_replace(source, target)

    def tracking_chmod(path: str | os.PathLike[str], mode: int) -> None:
        if Path(path).suffix == ".smsx":
            events.append("chmod")
        real_chmod(path, mode)

    monkeypatch.setattr("app.services.export_file.os.fsync", tracking_fsync)
    monkeypatch.setattr("app.services.export_file.os.replace", tracking_replace)
    monkeypatch.setattr("app.services.export_file.os.chmod", tracking_chmod)

    codec = ExportFileCodec(export_crypto(), tmp_path)
    path = await codec.write_csv(9, LEASE_ID, ("phone", "content"), rows())
    proof = codec.verify_ready(path, expected_task_id=9, expected_lease_id=LEASE_ID)
    assert proof.state == "complete"
    assert proof.row_count == 1
    assert proof.ciphertext_sha256
    assert "13800138000" not in path.read_bytes().decode("latin1")
    assert events[-4:] == ["fsync:file", "replace", "chmod", "fsync:dir"]


@pytest.mark.parametrize(
    "stage",
    ["after_file_fsync", "after_replace", "after_chmod", "after_directory_fsync"],
)
@pytest.mark.asyncio
async def test_export_write_kill_boundaries_leave_recoverable_state(
    tmp_path: Path,
    stage: str,
) -> None:
    codec = ExportFileCodec(
        export_crypto(),
        tmp_path,
        on_write_stage=crash_at(stage),
    )
    with pytest.raises(ExportWriteInterrupted, match=stage):
        await codec.write_csv(9, LEASE_ID, ("phone", "content"), rows())

    part = tmp_path / f"export-9-{LEASE_ID}.part"
    final = tmp_path / f"export-9-{LEASE_ID}.smsx"
    if stage == "after_file_fsync":
        assert part.exists() and not final.exists()
        return
    assert not part.exists()
    assert final.exists()
    recovered = ExportFileCodec(export_crypto(), tmp_path).find_reusable_final(9)
    assert recovered is not None
    assert recovered.path == final
    assert recovered.row_count == 1
