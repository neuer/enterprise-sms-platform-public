from __future__ import annotations

import base64
import struct
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag

from app.services.crypto import CryptoService
from app.services.import_file import (
    IMPORT_ID_SIZE,
    LENGTH_SIZE,
    MAGIC_V2,
    SIZE_SIZE,
    VERSION_SIZE,
    ImportFileCodec,
)


def crypto() -> CryptoService:
    key = base64.b64encode(b"k" * 32).decode("ascii")
    return CryptoService.from_secret_values(key, key)


def test_import_source_is_framed_encrypted_and_round_trips_in_memory(
    tmp_path: Path,
) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("11111111-1111-4111-8111-111111111111")
    plaintext = b"phone\n13800138000\n" + b"x" * 70_000

    relative = codec.stage(
        import_id,
        BytesIO(plaintext),
        size=len(plaintext),
        max_bytes=len(plaintext),
    )

    ciphertext = (tmp_path / relative).read_bytes()
    assert b"13800138000" not in ciphertext
    assert relative == f"import-{import_id}.smsx"
    temporary = codec.decrypt_to_memory(
        relative,
        expected_import_id=import_id,
        expected_size=len(plaintext),
        max_bytes=len(plaintext),
    )
    try:
        assert temporary.read() == plaintext
        assert temporary.name is None
    finally:
        temporary.close()


def test_import_source_detects_tampering_and_rejects_uncontrolled_paths(
    tmp_path: Path,
) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("22222222-2222-4222-8222-222222222222")
    relative = codec.stage(
        import_id,
        BytesIO(b"13800138000\n"),
        size=12,
        max_bytes=12,
    )
    path = tmp_path / relative
    payload = path.read_bytes()
    path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))

    with pytest.raises((InvalidTag, ValueError)):
        codec.decrypt_to_memory(
            relative,
            expected_import_id=import_id,
            expected_size=12,
            max_bytes=12,
        )
    with pytest.raises(ValueError, match="不受控"):
        codec.remove("../outside.smsx")


def test_import_source_rejects_size_drift_and_removes_partial_file(
    tmp_path: Path,
) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("33333333-3333-4333-8333-333333333333")

    with pytest.raises(ValueError, match="大小"):
        codec.stage(import_id, BytesIO(b"abc"), size=2, max_bytes=3)

    assert list(tmp_path.iterdir()) == []


def test_import_source_uses_smsi2_format_with_context_bound_frames(
    tmp_path: Path,
) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("44444444-4444-4444-8444-444444444444")
    relative = codec.stage(
        import_id,
        BytesIO(b"13800138000\n"),
        size=12,
        max_bytes=12,
    )
    ciphertext = (tmp_path / relative).read_bytes()
    assert ciphertext.startswith(MAGIC_V2)
    assert b"13800138000" not in ciphertext
    header_size = len(MAGIC_V2) + VERSION_SIZE + IMPORT_ID_SIZE + SIZE_SIZE
    declared_size = int.from_bytes(ciphertext[header_size - SIZE_SIZE : header_size], "big")
    assert declared_size == 12


def test_smsi2_rejects_reordered_frames(tmp_path: Path) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("55555555-5555-4555-8555-555555555555")
    plaintext = b"line-1\n" * 6_000 + b"line-2\n" * 6_000
    relative = codec.stage(
        import_id,
        BytesIO(plaintext),
        size=len(plaintext),
        max_bytes=len(plaintext),
    )
    raw = bytearray((tmp_path / relative).read_bytes())
    header_size = len(MAGIC_V2) + VERSION_SIZE + IMPORT_ID_SIZE + SIZE_SIZE
    first_len = struct.unpack(">I", bytes(raw[header_size : header_size + LENGTH_SIZE]))[0]
    first_start = header_size + LENGTH_SIZE
    second_len_pos = first_start + first_len
    second_len = struct.unpack(
        ">I",
        bytes(raw[second_len_pos : second_len_pos + LENGTH_SIZE]),
    )[0]
    second_start = second_len_pos + LENGTH_SIZE
    raw[first_start : first_start + first_len], raw[
        second_start : second_start + second_len
    ] = (
        raw[second_start : second_start + second_len],
        raw[first_start : first_start + first_len],
    )
    (tmp_path / relative).write_bytes(bytes(raw))

    with pytest.raises(ValueError, match="认证失败"):
        codec.decrypt_to_memory(
            relative,
            expected_import_id=import_id,
            expected_size=len(plaintext),
            max_bytes=len(plaintext),
        )


def test_smsi2_rejects_missing_terminal_frame(tmp_path: Path) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("66666666-6666-4666-8666-666666666666")
    relative = codec.stage(
        import_id,
        BytesIO(b"13800138000\n"),
        size=12,
        max_bytes=12,
    )
    raw = (tmp_path / relative).read_bytes()
    header_size = len(MAGIC_V2) + VERSION_SIZE + IMPORT_ID_SIZE + SIZE_SIZE
    first_len = struct.unpack(">I", raw[header_size : header_size + LENGTH_SIZE])[0]
    terminal_start = header_size + LENGTH_SIZE + first_len
    (tmp_path / relative).write_bytes(raw[:terminal_start])

    with pytest.raises(ValueError, match="缺少终止帧"):
        codec.decrypt_to_memory(
            relative,
            expected_import_id=import_id,
            expected_size=12,
            max_bytes=12,
        )


def test_smsi1_legacy_file_is_rejected_by_default(tmp_path: Path) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    import_id = UUID("77777777-7777-4777-8777-777777777777")
    path = tmp_path / f"import-{import_id}.smsx"
    path.write_bytes(b"SMSI1" + b"\x00\x01")

    with pytest.raises(ValueError, match="legacy import format rejected"):
        codec.decrypt_to_memory(
            path.name,
            expected_import_id=import_id,
            expected_size=0,
            max_bytes=1,
        )


def test_smsi2_rejects_cross_import_file_replacement(tmp_path: Path) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    first = UUID("88888888-8888-4888-8888-888888888888")
    second = UUID("99999999-9999-4999-9999-999999999999")
    plaintext = b"13800138000\n"
    first_relative = codec.stage(
        first,
        BytesIO(plaintext),
        size=len(plaintext),
        max_bytes=len(plaintext),
    )
    second_relative = codec.stage(
        second,
        BytesIO(plaintext),
        size=len(plaintext),
        max_bytes=len(plaintext),
    )

    # 把 B 的完整合法 SMSI2 密文放到 A 的受控文件名下。
    (tmp_path / first_relative).write_bytes((tmp_path / second_relative).read_bytes())

    with pytest.raises(ValueError, match="身份与任务不一致"):
        codec.decrypt_to_memory(
            first_relative,
            expected_import_id=first,
            expected_size=len(plaintext),
            max_bytes=len(plaintext),
        )


def test_smsi2_rejects_filename_identity_mismatch(tmp_path: Path) -> None:
    codec = ImportFileCodec(crypto(), tmp_path)
    first = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    second = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    relative = codec.stage(
        first,
        BytesIO(b"13800138000\n"),
        size=12,
        max_bytes=12,
    )

    with pytest.raises(ValueError, match="文件名与任务身份不一致"):
        codec.decrypt_to_memory(
            relative,
            expected_import_id=second,
            expected_size=12,
            max_bytes=12,
        )
