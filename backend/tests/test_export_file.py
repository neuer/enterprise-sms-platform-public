from __future__ import annotations

import base64
import csv
import io
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag

from app.services.crypto import CryptoService
from app.services.export_file import ExportFileCodec, _csv_cell

LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")


def crypto() -> CryptoService:
    key = base64.b64encode(b"e" * 32).decode()
    return CryptoService.from_secret_values(key, key)


async def rows() -> AsyncIterator[tuple[object, ...]]:
    yield ("13800138000", "=HYPERLINK(\"https://bad\")", "delivered")
    yield ("13900139000", "系统通知", "failed")


@pytest.mark.asyncio
async def test_chunked_export_is_always_ciphertext_and_streams_authenticated_csv(
    tmp_path: Path,
) -> None:
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)

    path = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())

    payload = path.read_bytes()
    assert payload.startswith(b"SMSX2")
    assert b"13800138000" not in payload
    assert "系统通知".encode() not in payload
    assert path.stat().st_mode & 0o777 == 0o600
    plaintext = b"".join(codec.iter_decrypted(path)).decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(plaintext)))
    assert parsed[0] == ["phone", "content", "status"]
    assert parsed[1] == ["13800138000", "'=HYPERLINK(\"https://bad\")", "delivered"]
    assert parsed[2] == ["13900139000", "系统通知", "failed"]
    assert payload.count(b"SMSX2") == 1


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(1,1)",
        "\t=HYPERLINK(\"https://example.invalid\")",
        "\r=1+1",
        "\n=1+1",
        "  =1+1",
        "\t  @SUM(1,1)",
    ],
)
def test_csv_cell_escapes_formula_after_whitespace_normalization(value: str) -> None:
    assert _csv_cell(value).startswith("'")


def test_csv_cell_keeps_normal_text_and_safe_hyphens() -> None:
    assert _csv_cell("正常通知") == "正常通知"
    assert _csv_cell("订单-123") == "订单-123"


@pytest.mark.asyncio
async def test_failed_write_removes_ciphertext_part_and_final_file(tmp_path: Path) -> None:
    async def broken() -> AsyncIterator[tuple[str]]:
        yield ("13800138000",)
        raise RuntimeError("boom")

    codec = ExportFileCodec(crypto(), tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        await codec.write_csv(7, LEASE_ID, ("phone",), broken())
    assert os.listdir(tmp_path) == []


@pytest.mark.asyncio
async def test_download_rejects_truncated_ciphertext_and_path_escape(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)
    path = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())
    path.write_bytes(path.read_bytes()[:-3])

    with pytest.raises(ValueError, match="truncated"):
        b"".join(codec.iter_decrypted(path))
    outside = tmp_path.parent / "outside.smsx"
    outside.write_bytes(b"SMSX1")
    with pytest.raises(ValueError, match="outside"):
        list(codec.iter_decrypted(outside))


def test_storage_directory_is_private(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    ExportFileCodec(crypto(), root)
    assert os.stat(root).st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_each_lease_has_isolated_part_and_final_paths(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    other = UUID("30000000-0000-4000-8000-000000000009")

    first = await codec.write_csv(9, LEASE_ID, ("phone",), rows())
    second = await codec.write_csv(9, other, ("phone",), rows())

    assert first != second
    assert str(LEASE_ID) in first.name and str(other) in second.name
    codec.remove(first)
    assert second.exists()


@pytest.mark.asyncio
async def test_export_detects_reordered_deleted_and_cross_file_frames(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)
    first = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())
    other_lease = UUID("30000000-0000-4000-8000-000000000009")
    second = await codec.write_csv(9, other_lease, ("phone", "content", "status"), rows())

    header_size = 7

    def frames(payload: bytes) -> list[bytes]:
        result: list[bytes] = []
        offset = header_size
        while offset < len(payload):
            length = int.from_bytes(payload[offset + 1 : offset + 5], "big")
            end = offset + 5 + length
            result.append(payload[offset:end])
            offset = end
        return result

    original = first.read_bytes()
    chunks = frames(original)
    assert len(chunks) >= 3 and chunks[-1].startswith(b"t")

    first.write_bytes(original[:header_size] + chunks[1] + chunks[0] + b"".join(chunks[2:]))
    with pytest.raises((InvalidTag, ValueError)):
        b"".join(codec.iter_decrypted(first))

    first.write_bytes(original[:header_size] + b"".join(chunks[:-2] + chunks[-1:]))
    with pytest.raises((InvalidTag, ValueError)):
        b"".join(codec.iter_decrypted(first))

    second_chunks = frames(second.read_bytes())
    first.write_bytes(
        original[:header_size]
        + second_chunks[0]
        + b"".join(chunks[1:])
    )
    with pytest.raises((InvalidTag, ValueError)):
        b"".join(codec.iter_decrypted(first))
