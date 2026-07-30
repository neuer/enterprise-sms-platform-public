from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag

from app.services.crypto import CryptoService
from app.services.import_file import ImportFileCodec


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

    with pytest.raises(InvalidTag):
        codec.decrypt_to_memory(relative, expected_size=12, max_bytes=12)
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
