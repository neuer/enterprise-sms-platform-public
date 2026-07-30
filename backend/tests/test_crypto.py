from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import given
from hypothesis import strategies as st

from app.services.crypto import (
    BOUND_ENVELOPE_MAGIC,
    CryptoService,
    EncryptionContext,
)


def b64(byte: bytes) -> str:
    return base64.b64encode(byte * 32).decode("ascii")


def keyring(active: int, keys: dict[int, str]) -> str:
    return json.dumps(
        {"active_version": active, "keys": {str(version): key for version, key in keys.items()}}
    )


def test_v1_bare_secrets_protect_phone_without_plaintext() -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))

    protected = service.protect_phone("13800138000")

    assert protected.key_version == 1
    assert protected.phone_mask == "138****8000"
    assert protected.phone_hmac == hmac.new(b"h" * 32, b"13800138000", hashlib.sha256).hexdigest()
    assert b"13800138000" not in protected.phone_enc
    assert (
        service.decrypt_phone(
            protected.phone_enc,
            protected.key_version,
            protected.phone_hmac,
        )
        == "13800138000"
    )


def test_service_reads_only_whitelisted_data_secret_names() -> None:
    requested: list[str] = []
    values = {"data_aes_key": b64(b"a"), "data_hmac_key": b64(b"h")}

    class FakeSettings:
        def credential(self, name: str) -> str:
            requested.append(name)
            return values[name]

    service = CryptoService.from_settings(FakeSettings())

    assert service.active_version == 1
    assert requested == ["data_aes_key", "data_hmac_key"]


def test_aes_gcm_uses_random_nonce_and_detects_tampering() -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))

    first = service.encrypt_text("同一内容")
    second = service.encrypt_text("同一内容")

    assert first.payload != second.payload
    assert len(first.payload) > 12
    assert service.decrypt_text(first.payload, first.key_version) == "同一内容"
    tampered = first.payload[:-1] + bytes([first.payload[-1] ^ 1])
    with pytest.raises(InvalidTag):
        service.decrypt_text(tampered, first.key_version)


def test_rotated_keyring_decrypts_old_and_new_versions() -> None:
    aes_v1 = b64(b"a")
    aes_v2 = b64(b"b")
    hmac_v1 = b64(b"h")
    hmac_v2 = b64(b"i")
    old_service = CryptoService.from_secret_values(aes_v1, hmac_v1)
    old_value = old_service.encrypt_text("历史密文")
    rotated = CryptoService.from_secret_values(
        keyring(2, {1: aes_v1, 2: aes_v2}),
        keyring(2, {1: hmac_v1, 2: hmac_v2}),
    )

    new_value = rotated.encrypt_text("当前密文")

    assert new_value.key_version == 2
    assert rotated.decrypt_text(old_value.payload, 1) == "历史密文"
    assert rotated.decrypt_text(new_value.payload, 2) == "当前密文"
    assert set(rotated.hmac_candidates("13800138000")) == {1, 2}


def test_packed_ciphertext_keeps_version_for_callback_secret_rotation() -> None:
    aes_v1 = b64(b"a")
    hmac_v1 = b64(b"h")
    old_service = CryptoService.from_secret_values(aes_v1, hmac_v1)
    packed = old_service.encrypt_packed_text("callback-secret")
    rotated = CryptoService.from_secret_values(
        keyring(2, {1: aes_v1, 2: b64(b"b")}),
        keyring(2, {1: hmac_v1, 2: b64(b"i")}),
    )

    assert int.from_bytes(packed[:2], "big") == 1
    assert rotated.decrypt_packed_text(packed) == "callback-secret"


def test_keyrings_must_match_and_keys_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        CryptoService.from_secret_values(base64.b64encode(b"short").decode(), b64(b"h"))

    with pytest.raises(ValueError, match="version"):
        CryptoService.from_secret_values(
            keyring(2, {1: b64(b"a"), 2: b64(b"b")}),
            keyring(1, {1: b64(b"h")}),
        )


@pytest.mark.parametrize("phone", ["", "1380013800", "23800138000", "1380013800a"])
def test_phone_mask_and_hmac_reject_invalid_numbers(phone: str) -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))

    with pytest.raises(ValueError, match="phone"):
        service.mask_phone(phone)
    with pytest.raises(ValueError, match="phone"):
        service.phone_hmac(phone)


def test_unknown_key_version_and_short_ciphertext_are_rejected() -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))

    with pytest.raises(ValueError, match="key version"):
        service.decrypt_text(b"x" * 32, 2)
    with pytest.raises(ValueError, match="ciphertext"):
        service.decrypt_text(b"too-short", 1)


def test_bound_envelope_rejects_cross_field_and_cross_object_transplant() -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))
    source = EncryptionContext("sms-content", "sms_batch", "send_content_enc", "batch-a")
    encrypted = service.encrypt_bound_text("敏感正文", source)

    assert (
        service.decrypt_bound_text(encrypted.payload, encrypted.key_version, source)
        == "敏感正文"
    )
    for replacement in (
        EncryptionContext("sms-content", "sms_batch", "content", "batch-a"),
        EncryptionContext("sms-content", "sms_batch", "send_content_enc", "batch-b"),
        EncryptionContext("callback-secret", "app", "callback_secret_enc", "batch-a"),
    ):
        with pytest.raises(InvalidTag):
            service.decrypt_bound_text(
                encrypted.payload,
                encrypted.key_version,
                replacement,
                allow_legacy=False,
            )


def test_bound_reader_dual_reads_legacy_then_reencrypts_without_plaintext_persistence() -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))
    context = EncryptionContext("vendor-raw", "raw_vendor_log", "payload_enc", "report:digest")
    legacy = service.encrypt_bytes(b'{"code":0}')

    plaintext = service.decrypt_bound_bytes(legacy.payload, legacy.key_version, context)
    migrated = service.encrypt_bound_bytes(plaintext, context)

    assert migrated.payload != legacy.payload
    assert service.decrypt_bound_bytes(
        migrated.payload,
        migrated.key_version,
        context,
        allow_legacy=False,
    ) == b'{"code":0}'
    with pytest.raises(ValueError, match="legacy"):
        service.decrypt_bound_bytes(
            legacy.payload,
            legacy.key_version,
            context,
            allow_legacy=False,
        )


@given(
    plaintext=st.binary(max_size=1024),
    object_id=st.text(min_size=1, max_size=40),
    flip=st.integers(min_value=0),
)
def test_bound_envelope_property_round_trip_domain_separation_and_tamper_detection(
    plaintext: bytes,
    object_id: str,
    flip: int,
) -> None:
    service = CryptoService.from_secret_values(b64(b"a"), b64(b"h"))
    context = EncryptionContext("phone", "sms_message", "phone_enc", object_id)
    encrypted = service.encrypt_bound_bytes(plaintext, context)

    assert (
        service.decrypt_bound_bytes(
            encrypted.payload,
            encrypted.key_version,
            context,
            allow_legacy=False,
        )
        == plaintext
    )
    with pytest.raises(InvalidTag):
        service.decrypt_bound_bytes(
            encrypted.payload,
            encrypted.key_version,
            EncryptionContext("phone", "sms_message", "phone_enc", object_id + ":other"),
            allow_legacy=False,
        )

    tampered = bytearray(encrypted.payload)
    encrypted_index = len(BOUND_ENVELOPE_MAGIC) + (
        flip % (len(tampered) - len(BOUND_ENVELOPE_MAGIC))
    )
    tampered[encrypted_index] ^= 1
    with pytest.raises(InvalidTag):
        service.decrypt_bound_bytes(
            bytes(tampered),
            encrypted.key_version,
            context,
            allow_legacy=False,
        )
