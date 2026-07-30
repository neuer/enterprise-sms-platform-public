from __future__ import annotations

import base64
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

NAME = "formal-name-sentinel"
KEY = "formal-key-sentinel"
NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)


def _seal(module, session, *, secret_name: str = NAME, secret_key: str = KEY):
    public_key = serialization.load_der_public_key(base64.b64decode(session.public_key))
    aes_key = os.urandom(32)
    nonce = os.urandom(12)
    aad = base64.b64decode(session.aad)
    plaintext = module.pack_credentials(secret_name, secret_key)
    wrapped = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext, aad)
    return module.SealedCredentialEnvelope(
        session_id=session.session_id,
        wrapped_key=base64.b64encode(wrapped).decode(),
        nonce=base64.b64encode(nonce).decode(),
        ciphertext=base64.b64encode(ciphertext).decode(),
        aad=base64.b64encode(aad).decode(),
        algorithm="RSA-OAEP-256+A256GCM",
    )


def test_session_is_120_seconds_single_use_and_decrypts_fixed_payload() -> None:
    import vendor_seal_sessions as seal

    manager = seal.SealSessionManager(clock=lambda: NOW)
    session = manager.create("install_credentials", "admin")

    credentials = manager.open(
        _seal(seal, session),
        operation="install_credentials",
        actor="admin",
    )

    assert session.expires_at == NOW + timedelta(seconds=120)
    assert credentials.secret_name == NAME and credentials.secret_key == KEY
    assert NAME not in repr(credentials) and KEY not in repr(credentials)
    with pytest.raises(seal.SealSessionError, match="无效或已过期"):
        manager.open(
            _seal(seal, session),
            operation="install_credentials",
            actor="admin",
        )


@pytest.mark.parametrize("tamper", ("wrapped_key", "nonce", "ciphertext", "aad"))
def test_tamper_is_rejected_without_plaintext_or_crypto_details(tamper: str) -> None:
    import vendor_seal_sessions as seal

    manager = seal.SealSessionManager(clock=lambda: NOW)
    session = manager.create("install_credentials", "admin")
    envelope = _seal(seal, session)
    changed = {field: getattr(envelope, field) for field in envelope.__dataclass_fields__}
    changed[tamper] = base64.b64encode(b"tampered").decode()

    with pytest.raises(seal.SealSessionError) as captured:
        manager.open(
            seal.SealedCredentialEnvelope(**changed),
            operation="install_credentials",
            actor="admin",
        )

    rendered = str(captured.value)
    assert NAME not in rendered and KEY not in rendered
    assert "InvalidTag" not in rendered and "Encryption/decryption failed" not in rendered


def test_expired_session_is_consumed_and_never_reusable() -> None:
    import vendor_seal_sessions as seal

    now = [NOW]
    manager = seal.SealSessionManager(clock=lambda: now[0])
    session = manager.create("rotate_credentials", "admin")
    envelope = _seal(seal, session)
    now[0] += timedelta(seconds=121)

    with pytest.raises(seal.SealSessionError, match="无效或已过期"):
        manager.open(envelope, operation="rotate_credentials", actor="admin")
    now[0] = NOW
    with pytest.raises(seal.SealSessionError, match="无效或已过期"):
        manager.open(envelope, operation="rotate_credentials", actor="admin")


@pytest.mark.parametrize(
    ("operation", "actor"),
    (("rotate_credentials", "admin"), ("install_credentials", "other-admin")),
)
def test_session_rejects_cross_operation_or_cross_actor_envelope(
    operation: str,
    actor: str,
) -> None:
    import vendor_seal_sessions as seal

    manager = seal.SealSessionManager(clock=lambda: NOW)
    session = manager.create("install_credentials", "admin")

    with pytest.raises(seal.SealSessionError):
        manager.open(_seal(seal, session), operation=operation, actor=actor)


@pytest.mark.parametrize(
    ("secret_name", "secret_key"),
    (("", "key"), ("name\n", "key"), ("name", "key\x00"), ("x" * 1025, "key")),
)
def test_fixed_credential_payload_rejects_empty_control_and_oversize_values(
    secret_name: str,
    secret_key: str,
) -> None:
    import vendor_seal_sessions as seal

    with pytest.raises(seal.SealSessionError):
        seal.pack_credentials(secret_name, secret_key)
