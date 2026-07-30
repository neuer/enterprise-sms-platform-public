#!/usr/bin/env python3
"""root agent 内存中的单次 RSA-OAEP/AES-GCM 厂商凭据封装会话。"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SEAL_SESSION_TTL_SECONDS = 120
MAX_CREDENTIAL_BYTES = 1024
ALGORITHM = "RSA-OAEP-256+A256GCM"
_HEADER = struct.Struct("!HH")


class SealSessionError(ValueError):
    """封装会话或密文无效；异常不暴露密码学与明文细节。"""


@dataclass(frozen=True, slots=True)
class SealSession:
    session_id: str
    public_key: str
    expires_at: datetime
    aad: str


@dataclass(frozen=True, slots=True)
class SealedCredentialEnvelope:
    session_id: str
    wrapped_key: str
    nonce: str
    ciphertext: str
    aad: str
    algorithm: str


@dataclass(frozen=True, slots=True, repr=False)
class VendorCredentials:
    secret_name: str
    secret_key: str

    def __repr__(self) -> str:
        return "VendorCredentials(<redacted>)"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _context(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 256
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise SealSessionError("seal session 上下文无效")
    return value


def seal_aad(
    session_id: str,
    operation: str,
    actor: str,
    expires_at: datetime,
) -> bytes:
    """用规范 JSON 同时绑定会话、操作、操作者与到期时间。"""

    if operation not in {"install_credentials", "rotate_credentials"}:
        raise SealSessionError("seal session 上下文无效")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise SealSessionError("seal session 上下文无效")
    document = {
        "actor": _context(actor),
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "operation": operation,
        "session_id": _context(session_id),
    }
    return (
        "sms-platform:vendor-credentials:v2:".encode("ascii")
        + json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _credential_bytes(value: str) -> bytes:
    if type(value) is not str:
        raise SealSessionError("厂商凭据格式无效")
    encoded = value.encode("utf-8")
    if (
        not encoded
        or len(encoded) > MAX_CREDENTIAL_BYTES
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise SealSessionError("厂商凭据格式无效")
    return encoded


def pack_credentials(secret_name: str, secret_key: str) -> bytes:
    name = _credential_bytes(secret_name)
    key = _credential_bytes(secret_key)
    return _HEADER.pack(len(name), len(key)) + name + key


def _unpack_credentials(payload: bytes) -> VendorCredentials:
    if len(payload) < _HEADER.size:
        raise SealSessionError("凭据封装无效")
    name_length, key_length = _HEADER.unpack(payload[: _HEADER.size])
    if len(payload) != _HEADER.size + name_length + key_length:
        raise SealSessionError("凭据封装无效")
    try:
        name = payload[_HEADER.size : _HEADER.size + name_length].decode("utf-8")
        key = payload[_HEADER.size + name_length :].decode("utf-8")
    except UnicodeError:
        raise SealSessionError("凭据封装无效") from None
    _credential_bytes(name)
    _credential_bytes(key)
    return VendorCredentials(name, key)


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise SealSessionError("凭据封装无效") from None


class SealSessionManager:
    """私钥只存在于 root agent 内存，并在首次 open 尝试时即销毁。"""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock
        self._sessions: dict[
            str,
            tuple[RSAPrivateKey, datetime, str, str, bytes],
        ] = {}

    def create(self, operation: str, actor: str) -> SealSession:
        now = self.clock()
        self._sessions = {
            key: value for key, value in self._sessions.items() if value[1] > now
        }
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        session_id = os.urandom(32).hex()
        expires_at = now + timedelta(seconds=SEAL_SESSION_TTL_SECONDS)
        aad = seal_aad(session_id, operation, actor, expires_at)
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._sessions[session_id] = (
            private_key,
            expires_at,
            operation,
            _context(actor),
            aad,
        )
        return SealSession(
            session_id,
            base64.b64encode(public_der).decode("ascii"),
            expires_at,
            base64.b64encode(aad).decode("ascii"),
        )

    def open(
        self,
        envelope: SealedCredentialEnvelope,
        *,
        operation: str,
        actor: str,
    ) -> VendorCredentials:
        session = self._sessions.pop(envelope.session_id, None)
        if session is None or session[1] <= self.clock():
            raise SealSessionError("seal session 无效或已过期")
        private_key = session[0]
        try:
            if (operation, actor) != (session[2], session[3]):
                raise SealSessionError("凭据封装无效")
            if envelope.algorithm != ALGORITHM:
                raise SealSessionError("凭据封装无效")
            aad = _decode(envelope.aad)
            if aad != session[4]:
                raise SealSessionError("凭据封装无效")
            wrapped_key = _decode(envelope.wrapped_key)
            nonce = _decode(envelope.nonce)
            ciphertext = _decode(envelope.ciphertext)
            if len(nonce) != 12:
                raise SealSessionError("凭据封装无效")
            aes_key = private_key.decrypt(
                wrapped_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            if len(aes_key) != 32:
                raise SealSessionError("凭据封装无效")
            plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, aad)
            return _unpack_credentials(plaintext)
        except SealSessionError:
            raise
        except (ValueError, TypeError, InvalidTag):
            raise SealSessionError("凭据封装无效") from None
