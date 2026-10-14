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


@pytest.mark.parametrize("tamper", ("wrapped_key", "nonce", "ciphertext", "aad", "algorithm"))
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
    with pytest.raises(seal.SealSessionError):
        manager.open(envelope, operation="install_credentials", actor="admin")


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


def test_invalid_actor_is_rejected_before_rsa(monkeypatch: pytest.MonkeyPatch) -> None:
    import vendor_seal_sessions as seal

    generated = []
    original = seal.rsa.generate_private_key

    def generate(**kwargs):
        generated.append(True)
        return original(**kwargs)

    monkeypatch.setattr(seal.rsa, "generate_private_key", generate)
    with pytest.raises(seal.SealSessionError):
        seal.SealSessionManager().create("install_credentials", "a" * 257)
    assert not generated


def test_pruning_cannot_restore_a_concurrently_consumed_session(monkeypatch) -> None:
    """真实 RSA、双 create 与 open/清理交错；密钥生成期间共享锁必须可用。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    import vendor_seal_sessions as seal

    now, steady = [NOW], [0.0]
    manager = seal.SealSessionManager(clock=lambda: now[0], steady_clock=lambda: steady[0])
    original = manager.create("install_credentials", "admin")
    envelope = _seal(seal, original)
    generating, proceed = Barrier(3), Event()
    generate = seal.rsa.generate_private_key

    def slow_generate(**kwargs):
        generating.wait(timeout=3)
        assert proceed.wait(3)
        return generate(**kwargs)

    monkeypatch.setattr(seal.rsa, "generate_private_key", slow_generate)
    with ThreadPoolExecutor(max_workers=3) as pool:
        creates = [
            pool.submit(manager.create, "install_credentials", actor)
            for actor in ("first-admin", "second-admin")
        ]
        try:
            generating.wait(timeout=3)
            opened = pool.submit(
                manager.open, envelope, operation="install_credentials", actor="admin"
            )
            opened.result(timeout=1)
            now[0] += timedelta(seconds=121)
            steady[0] += 121
            pool.submit(manager.cleanup).result(timeout=1)
        finally:
            proceed.set()
        sessions = [created.result(timeout=3) for created in creates]
    assert len(manager._sessions) == 2
    assert not manager._reserved
    for session, actor in zip(sessions, ("first-admin", "second-admin"), strict=True):
        assert session.expires_at == now[0] + timedelta(seconds=120)
        manager.open(_seal(seal, session), operation="install_credentials", actor=actor)
    with pytest.raises(seal.SealSessionError):
        manager.open(envelope, operation="install_credentials", actor="admin")


def test_concurrent_open_has_at_most_one_success() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import vendor_seal_sessions as seal

    manager = seal.SealSessionManager()
    session = manager.create("install_credentials", "admin")
    envelope = _seal(seal, session)
    gate = Barrier(8)

    def attempt():
        gate.wait(timeout=3)
        try:
            manager.open(envelope, operation="install_credentials", actor="admin")
            return True
        except seal.SealSessionError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: attempt(), range(8))) == 1
    assert not manager._sessions


@pytest.mark.parametrize("failure", ["generate", "encode"])
def test_create_failure_releases_reservation_and_does_not_publish(monkeypatch, failure) -> None:
    import vendor_seal_sessions as seal

    class BrokenKey:
        def public_key(self):
            raise ValueError("synthetic-encoding-failure")

    def broken_generate(**_kwargs):
        if failure == "generate":
            raise ValueError("synthetic-generation-failure")
        return BrokenKey()

    manager = seal.SealSessionManager()
    monkeypatch.setattr(seal.rsa, "generate_private_key", broken_generate)
    with pytest.raises(seal.SealSessionError, match="创建失败"):
        manager.create("install_credentials", "admin")
    assert not manager._reserved and not manager._sessions
    assert len(manager._creates) == 1


def test_active_and_rate_limits_are_independent_and_recover() -> None:
    from dataclasses import replace

    import vendor_seal_sessions as seal

    now, steady = [NOW], [0.0]
    limits = replace(
        seal.AgentLimits(), max_sessions=2, max_actor_sessions=1, max_creates=3, max_actor_creates=2
    )
    manager = seal.SealSessionManager(
        clock=lambda: now[0], steady_clock=lambda: steady[0], limits=limits
    )
    first = manager.create("install_credentials", "admin")
    with pytest.raises(seal.SealSessionError):
        manager.create("install_credentials", "admin")
    manager.create("install_credentials", "other")
    with pytest.raises(seal.SealSessionError):
        manager.create("install_credentials", "third")
    manager.open(_seal(seal, first), operation="install_credentials", actor="admin")
    second = manager.create("install_credentials", "admin")
    manager.open(_seal(seal, second), operation="install_credentials", actor="admin")
    for actor in ("admin", "third"):
        with pytest.raises(seal.SealSessionError):
            manager.create("install_credentials", actor)
    steady[0] += 61
    recovered = manager.create("install_credentials", "admin")
    assert recovered.session_id in manager._sessions
    steady[0] += 121
    now[0] -= timedelta(seconds=120)
    manager.cleanup()
    assert not manager._sessions and not manager._reserved


@pytest.mark.parametrize("actor", ["wrong", "a" * 257])
def test_bad_first_open_always_consumes(actor) -> None:
    import vendor_seal_sessions as seal

    manager = seal.SealSessionManager()
    session = manager.create("install_credentials", "admin")
    envelope = _seal(seal, session)
    with pytest.raises(seal.SealSessionError):
        manager.open(envelope, operation="install_credentials", actor=actor)
    with pytest.raises(seal.SealSessionError):
        manager.open(envelope, operation="install_credentials", actor="admin")
