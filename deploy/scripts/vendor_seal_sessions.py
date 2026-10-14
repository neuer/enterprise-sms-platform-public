#!/usr/bin/env python3
"""root agent 内存中的单次 RSA-OAEP/AES-GCM 厂商凭据封装会话。"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic

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
class AgentLimits:
    """联调代理固定资源预算；仅构造注入用于小容量隔离验收。"""

    api_connections: int = 16
    status_connections: int = 4
    frame_timeout_s: float = 2.0
    write_timeout_s: float = 1.0
    close_timeout_s: float = 0.25
    crypto_workers: int = 2
    crypto_pending: int = 2
    operation_workers: int = 2
    operation_pending: int = 4
    status_workers: int = 2
    status_pending: int = 4
    max_inflight_operations: int = 16
    max_sessions: int = 32
    max_actor_sessions: int = 4
    max_creates: int = 16
    max_actor_creates: int = 4
    create_window_s: float = 60.0
    cleanup_interval_s: float = 5.0

    def __post_init__(self) -> None:
        for name in (
            "api_connections",
            "status_connections",
            "crypto_workers",
            "crypto_pending",
            "operation_workers",
            "operation_pending",
            "status_workers",
            "status_pending",
            "max_inflight_operations",
            "max_sessions",
            "max_actor_sessions",
            "max_creates",
            "max_actor_creates",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 128:
                raise ValueError("agent resource limit invalid")
        for name in (
            "frame_timeout_s",
            "write_timeout_s",
            "close_timeout_s",
            "create_window_s",
            "cleanup_interval_s",
        ):
            if not 0 < getattr(self, name) <= 120:
                raise ValueError("agent resource deadline invalid")
        for lane in ("crypto", "operation", "status"):
            if getattr(self, f"{lane}_workers") > getattr(self, f"{lane}_pending"):
                raise ValueError("agent executor limit invalid")
        if self.max_actor_sessions > self.max_sessions or self.max_actor_creates > self.max_creates:
            raise ValueError("agent actor limit invalid")


@dataclass(frozen=True, slots=True, repr=False)
class _StoredSession:
    private_key: RSAPrivateKey
    expires_at: datetime
    operation: str
    actor: str
    aad: bytes
    deadline: float
    delivery_started: bool = False


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

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        steady_clock: Callable[[], float] = monotonic,
        limits: AgentLimits | None = None,
    ) -> None:
        self.clock = clock
        self.steady_clock = steady_clock
        self.limits = limits or AgentLimits()
        self._lock = Lock()
        self._sessions: dict[str, _StoredSession] = {}
        self._reserved: Counter[str] = Counter()
        self._creates: deque[tuple[float, str]] = deque()

    def _prune_locked(self) -> None:
        now, steady = self.clock(), self.steady_clock()
        for key in tuple(self._sessions):
            session = self._sessions[key]
            if session.expires_at <= now or session.deadline <= steady:
                del self._sessions[key]
        while self._creates and self._creates[0][0] <= steady - self.limits.create_window_s:
            self._creates.popleft()

    def cleanup(self) -> None:
        """周期清理只删除到期引用，不驱逐有效会话，不声称物理内存擦除。"""
        with self._lock:
            self._prune_locked()

    def discard(self, session_id: str) -> None:
        """仅在确认尚未开始交付时回收生成结果。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and not session.delivery_started:
                del self._sessions[session_id]

    def mark_delivery_started(self, session_id: str) -> None:
        """任一原请求或幂等重放开始写响应后，取消不能删除该有效会话。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                self._sessions[session_id] = replace(session, delivery_started=True)

    def is_active(self, session_id: str, *, operation: str, actor: str) -> bool:
        """幂等公钥响应只有对应私钥仍有效且主体一致时才能复用。"""
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            return session is not None and (session.operation, session.actor) == (operation, actor)

    def _release_locked(self, actor: str) -> None:
        self._reserved[actor] -= 1
        if self._reserved[actor] == 0:
            del self._reserved[actor]

    def create(self, operation: str, actor: str) -> SealSession:
        actor = _context(actor)
        if operation not in {"install_credentials", "rotate_credentials"}:
            raise SealSessionError("seal session 上下文无效")
        with self._lock:
            self._prune_locked()
            reserved = sum(self._reserved.values())
            actor_active = sum(session.actor == actor for session in self._sessions.values())
            if (
                len(self._sessions) + reserved >= self.limits.max_sessions
                or actor_active + self._reserved[actor] >= self.limits.max_actor_sessions
                or reserved >= self.limits.crypto_workers
                or len(self._creates) >= self.limits.max_creates
                or sum(item[1] == actor for item in self._creates) >= self.limits.max_actor_creates
            ):
                raise SealSessionError("seal session 容量暂不可用")
            self._reserved[actor] += 1
            self._creates.append((self.steady_clock(), actor))
        reserved_slot = True
        try:
            # 昂贵工作不持共享锁。所有失败路径归还预留，失败仍计入创建频率。
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            public_der = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            public_key = base64.b64encode(public_der).decode("ascii")
            session_id = os.urandom(32).hex()
            with self._lock:
                expires_at = self.clock() + timedelta(seconds=SEAL_SESSION_TTL_SECONDS)
                aad = seal_aad(session_id, operation, actor, expires_at)
                response = SealSession(
                    session_id, public_key, expires_at, base64.b64encode(aad).decode("ascii")
                )
                if session_id in self._sessions:
                    raise SealSessionError("seal session 创建失败")
                self._sessions[session_id] = _StoredSession(
                    private_key,
                    expires_at,
                    operation,
                    actor,
                    aad,
                    self.steady_clock() + SEAL_SESSION_TTL_SECONDS,
                )
                self._release_locked(actor)
                reserved_slot = False
                return response
        except SealSessionError:
            raise
        except Exception:
            raise SealSessionError("seal session 创建失败") from None
        finally:
            if reserved_slot:
                with self._lock:
                    self._release_locked(actor)

    def open(
        self,
        envelope: SealedCredentialEnvelope,
        *,
        operation: str,
        actor: str,
    ) -> VendorCredentials:
        with self._lock:
            session = self._sessions.pop(envelope.session_id, None)
        if (
            session is None
            or session.expires_at <= self.clock()
            or session.deadline <= self.steady_clock()
        ):
            raise SealSessionError("seal session 无效或已过期")
        private_key = session.private_key
        try:
            if (operation, actor) != (session.operation, session.actor):
                raise SealSessionError("凭据封装无效")
            if envelope.algorithm != ALGORITHM:
                raise SealSessionError("凭据封装无效")
            aad = _decode(envelope.aad)
            if aad != session.aad:
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
