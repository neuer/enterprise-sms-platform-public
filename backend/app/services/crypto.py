"""手机号与敏感载荷的字段级加密、索引和掩码。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import get_settings

LOGGER = logging.getLogger(__name__)
PHONE_PATTERN = re.compile(r"^1\d{10}$")
NONCE_SIZE = 12
TAG_SIZE = 16
MAX_KEY_VERSION = 32767
KEY_VERSION_BYTES = 2
BOUND_ENVELOPE_MAGIC = b"SME2"
BOUND_ENVELOPE_SCHEMA_VERSION = 2
CONTEXT_COMPONENT_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class CredentialSettings(Protocol):
    """加密服务所需的最小 settings 接口。"""

    def credential(self, name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """需要与 key_version 一起持久化的密文。"""

    payload: bytes
    key_version: int


@dataclass(frozen=True, slots=True)
class ProtectedPhone:
    """逐号码持久化唯一允许的四元组。"""

    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int


@dataclass(frozen=True, slots=True)
class EncryptionContext:
    """不可歧义地绑定密文的数据域、存储位置和不可变对象标识。"""

    domain: str
    table: str
    column: str
    object_id: str

    def canonical_aad(self, key_version: int) -> bytes:
        """生成稳定 JSON AAD；任一上下文分量变化都会导致 GCM 认证失败。"""

        for label, value in (
            ("domain", self.domain),
            ("table", self.table),
            ("column", self.column),
        ):
            if CONTEXT_COMPONENT_PATTERN.fullmatch(value) is None:
                raise ValueError(f"encryption context {label} is invalid")
        if not self.object_id or len(self.object_id.encode("utf-8")) > 256:
            raise ValueError("encryption context object_id is invalid")
        document = {
            "column": self.column,
            "domain": self.domain,
            "key_version": key_version,
            "object_id": self.object_id,
            "schema_version": BOUND_ENVELOPE_SCHEMA_VERSION,
            "table": self.table,
        }
        return (
            b"sms-platform:envelope:"
            + json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )


@dataclass(frozen=True, slots=True)
class _ParsedKeyring:
    active_version: int
    keys: dict[int, bytes]


def _decode_key(encoded: Any, *, label: str, version: int) -> bytes:
    if not isinstance(encoded, str):
        raise ValueError(f"{label} key version {version} must be base64 text")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} key version {version} is not valid base64") from exc
    if len(key) != 32:
        raise ValueError(f"{label} key version {version} must decode to 32 bytes")
    return key


def _parse_keyring(secret: str, *, label: str) -> _ParsedKeyring:
    """解析裸 v1 key 或同 secret 文件内的版本化 JSON keyring。"""

    if not secret.startswith("{"):
        return _ParsedKeyring(1, {1: _decode_key(secret, label=label, version=1)})
    try:
        document = json.loads(secret)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} keyring is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} keyring must be an object")
    active = document.get("active_version")
    raw_keys = document.get("keys")
    if not isinstance(active, int) or isinstance(active, bool):
        raise ValueError(f"{label} keyring active_version must be an integer")
    if not 1 <= active <= MAX_KEY_VERSION:
        raise ValueError(f"{label} keyring active key version is out of range")
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise ValueError(f"{label} keyring keys must be a non-empty object")

    keys: dict[int, bytes] = {}
    for raw_version, encoded in raw_keys.items():
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} keyring version must be an integer") from exc
        if str(version) != str(raw_version) or not 1 <= version <= MAX_KEY_VERSION:
            raise ValueError(f"{label} keyring version is invalid: {raw_version}")
        keys[version] = _decode_key(encoded, label=label, version=version)
    if active not in keys:
        raise ValueError(f"{label} keyring active key version is missing")
    return _ParsedKeyring(active, keys)


class CryptoService:
    """以同版本 AES/HMAC keyring 保护敏感字段。"""

    def __init__(
        self,
        *,
        aes_keys: dict[int, bytes],
        hmac_keys: dict[int, bytes],
        active_version: int,
    ) -> None:
        if set(aes_keys) != set(hmac_keys) or active_version not in aes_keys:
            raise ValueError("AES/HMAC keyring version mismatch")
        self._aes_keys = dict(aes_keys)
        self._hmac_keys = dict(hmac_keys)
        self.active_version = active_version

    @property
    def key_versions(self) -> tuple[int, ...]:
        """可供认证尝试的 keyring 版本；不得只信任未认证 header 里的版本号。"""

        return tuple(sorted(self._aes_keys))

    @classmethod
    def from_secret_values(cls, aes_secret: str, hmac_secret: str) -> CryptoService:
        """从两个 Docker secret 文件内容构造服务。"""

        aes_ring = _parse_keyring(aes_secret, label="AES")
        hmac_ring = _parse_keyring(hmac_secret, label="HMAC")
        if (
            aes_ring.active_version != hmac_ring.active_version
            or set(aes_ring.keys) != set(hmac_ring.keys)
        ):
            raise ValueError("AES/HMAC keyring version mismatch")
        return cls(
            aes_keys=aes_ring.keys,
            hmac_keys=hmac_ring.keys,
            active_version=aes_ring.active_version,
        )

    @classmethod
    def from_settings(cls, settings: CredentialSettings) -> CryptoService:
        """从 settings 白名单接口读取数据密钥，不接触环境明文。"""

        return cls.from_secret_values(
            settings.credential("data_aes_key"),
            settings.credential("data_hmac_key"),
        )

    @staticmethod
    def _aad(version: int) -> bytes:
        return f"sms-platform:data:v{version}".encode("ascii")

    def _aes_key(self, version: int) -> bytes:
        try:
            return self._aes_keys[version]
        except KeyError:
            raise ValueError(f"unknown key version: {version}") from None

    def _hmac_key(self, version: int) -> bytes:
        try:
            return self._hmac_keys[version]
        except KeyError:
            raise ValueError(f"unknown key version: {version}") from None

    def encrypt_bytes_legacy(self, plaintext: bytes) -> EncryptedValue:
        """旧版通用 AAD 加密；禁止新持久化调用，仅历史兼容。"""

        version = self.active_version
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(self._aes_key(version)).encrypt(
            nonce,
            plaintext,
            self._aad(version),
        )
        return EncryptedValue(nonce + ciphertext, version)

    def decrypt_bytes_legacy(self, payload: bytes, key_version: int) -> bytes:
        """解密旧版通用 AAD 密文；用于迁移期双读。"""

        key = self._aes_key(key_version)
        if len(payload) < NONCE_SIZE + TAG_SIZE:
            raise ValueError("ciphertext payload is too short")
        nonce, ciphertext = payload[:NONCE_SIZE], payload[NONCE_SIZE:]
        return AESGCM(key).decrypt(nonce, ciphertext, self._aad(key_version))

    def encrypt_bound_bytes(
        self,
        plaintext: bytes,
        context: EncryptionContext,
    ) -> EncryptedValue:
        """使用 v2 envelope 加密并把密文绑定到明确业务上下文。"""

        version = self.active_version
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(self._aes_key(version)).encrypt(
            nonce,
            plaintext,
            context.canonical_aad(version),
        )
        return EncryptedValue(BOUND_ENVELOPE_MAGIC + nonce + ciphertext, version)

    def decrypt_bound_bytes(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = False,
    ) -> bytes:
        """读取 v2 上下文密文；legacy 兼容只能通过受控迁移接口显式开启。"""

        if payload.startswith(BOUND_ENVELOPE_MAGIC):
            encrypted = payload[len(BOUND_ENVELOPE_MAGIC) :]
            if len(encrypted) < NONCE_SIZE + TAG_SIZE:
                raise ValueError("bound ciphertext payload is too short")
            nonce, ciphertext = encrypted[:NONCE_SIZE], encrypted[NONCE_SIZE:]
            return AESGCM(self._aes_key(key_version)).decrypt(
                nonce,
                ciphertext,
                context.canonical_aad(key_version),
            )
        if not allow_legacy:
            raise ValueError("legacy ciphertext requires controlled migration")
        return self.decrypt_bytes_legacy(payload, key_version)

    def decrypt_bound_with_legacy_migration(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
    ) -> bytes:
        """受控迁移读取：仅迁移任务调用，并记录不含敏感内容的可观测日志。"""

        LOGGER.warning(
            "legacy bound ciphertext migration read domain=%s table=%s column=%s key_version=%s",
            context.domain,
            context.table,
            context.column,
            key_version,
        )
        return self.decrypt_bound_bytes(
            payload,
            key_version,
            context,
            allow_legacy=True,
        )

    def encrypt_text(self, plaintext: str) -> EncryptedValue:
        """以 UTF-8 加密文本。"""

        return self.encrypt_bytes_legacy(plaintext.encode("utf-8"))

    def decrypt_text(self, payload: bytes, key_version: int) -> str:
        """解密并严格按 UTF-8 还原文本。"""

        return self.decrypt_bytes_legacy(payload, key_version).decode("utf-8")

    def encrypt_bound_text(
        self,
        plaintext: str,
        context: EncryptionContext,
    ) -> EncryptedValue:
        """以 UTF-8 加密上下文绑定文本。"""

        return self.encrypt_bound_bytes(plaintext.encode("utf-8"), context)

    def decrypt_bound_text(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = False,
    ) -> str:
        """解密上下文绑定文本，并严格按 UTF-8 还原。"""

        return self.decrypt_bound_bytes(
            payload,
            key_version,
            context,
            allow_legacy=allow_legacy,
        ).decode("utf-8")

    def decrypt_bound_text_with_legacy_migration(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
    ) -> str:
        """受控迁移读取文本；仅供白名单迁移任务使用。"""

        return self.decrypt_bound_with_legacy_migration(
            payload,
            key_version,
            context,
        ).decode("utf-8")

    def encrypt_packed_text_legacy(self, plaintext: str) -> bytes:
        """把版本头与旧版 AES-GCM 密文打包；禁止新持久化调用。"""

        encrypted = self.encrypt_text(plaintext)
        return encrypted.key_version.to_bytes(KEY_VERSION_BYTES, "big") + encrypted.payload

    def decrypt_packed_text_legacy(self, packed: bytes) -> str:
        """从单字段旧版密文解析版本并解密。"""

        if len(packed) <= KEY_VERSION_BYTES:
            raise ValueError("packed ciphertext payload is too short")
        version = int.from_bytes(packed[:KEY_VERSION_BYTES], "big")
        return self.decrypt_text(packed[KEY_VERSION_BYTES:], version)

    def encrypt_bound_packed_text(
        self,
        plaintext: str,
        context: EncryptionContext,
    ) -> bytes:
        """把 key version 与 v2 上下文密文打包到单个 BYTEA 字段。"""

        encrypted = self.encrypt_bound_text(plaintext, context)
        return encrypted.key_version.to_bytes(KEY_VERSION_BYTES, "big") + encrypted.payload

    def decrypt_bound_packed_text(
        self,
        packed: bytes,
        context: EncryptionContext,
        *,
        allow_legacy: bool = False,
    ) -> str:
        """解密单字段上下文密文，并兼容迁移前历史值。"""

        if len(packed) <= KEY_VERSION_BYTES:
            raise ValueError("packed ciphertext payload is too short")
        version = int.from_bytes(packed[:KEY_VERSION_BYTES], "big")
        return self.decrypt_bound_text(
            packed[KEY_VERSION_BYTES:],
            version,
            context,
            allow_legacy=allow_legacy,
        )

    def decrypt_bound_packed_text_with_legacy_migration(
        self,
        packed: bytes,
        context: EncryptionContext,
    ) -> str:
        """受控迁移读取打包密文；仅供白名单迁移任务使用。"""

        if len(packed) <= KEY_VERSION_BYTES:
            raise ValueError("packed ciphertext payload is too short")
        version = int.from_bytes(packed[:KEY_VERSION_BYTES], "big")
        return self.decrypt_bound_text_with_legacy_migration(
            packed[KEY_VERSION_BYTES:],
            version,
            context,
        )

    @staticmethod
    def _validate_phone(phone: str) -> None:
        if PHONE_PATTERN.fullmatch(phone) is None:
            raise ValueError("phone must match ^1\\d{10}$")

    @classmethod
    def mask_phone(cls, phone: str) -> str:
        """生成固定 11 位的列表展示掩码。"""

        cls._validate_phone(phone)
        return f"{phone[:3]}****{phone[-4:]}"

    def phone_hmac(self, phone: str, key_version: int | None = None) -> str:
        """用指定或活动版本生成可索引的 HMAC-SHA256 hex。"""

        self._validate_phone(phone)
        version = self.active_version if key_version is None else key_version
        return hmac.new(
            self._hmac_key(version),
            phone.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def hmac_candidates(self, phone: str) -> dict[int, str]:
        """为轮换期精确查询生成所有可用版本索引值。"""

        self._validate_phone(phone)
        return {version: self.phone_hmac(phone, version) for version in sorted(self._hmac_keys)}

    def idempotency_fingerprint(
        self,
        canonical_request: bytes,
        *,
        key_version: int | None = None,
    ) -> str:
        """用版本化 HMAC 固化请求等价性，避免数据库摘要成为离线枚举 oracle。"""

        if not isinstance(canonical_request, bytes) or not canonical_request:
            raise ValueError("canonical request must be non-empty bytes")
        version = self.active_version if key_version is None else key_version
        domain = f"sms-platform:idempotency-request:v1:key:{version}\0".encode("ascii")
        return hmac.new(
            self._hmac_key(version),
            domain + canonical_request,
            hashlib.sha256,
        ).hexdigest()

    def stable_hmac_fingerprint(
        self,
        canonical_value: bytes,
        *,
        domain: str,
    ) -> tuple[int, str]:
        """用最早保留版本生成跨活动密钥轮换稳定的领域指纹。"""

        if not isinstance(canonical_value, bytes) or not canonical_value:
            raise ValueError("canonical value must be non-empty bytes")
        if CONTEXT_COMPONENT_PATTERN.fullmatch(domain) is None:
            raise ValueError("fingerprint domain is invalid")
        version = min(self._hmac_keys)
        prefix = f"sms-platform:fingerprint:{domain}:v1:key:{version}\0".encode("ascii")
        return (
            version,
            hmac.new(
                self._hmac_key(version),
                prefix + canonical_value,
                hashlib.sha256,
            ).hexdigest(),
        )

    @property
    def hmac_versions(self) -> frozenset[int]:
        """只暴露索引版本集合，供持久化投影做完整性校验。"""

        return frozenset(self._hmac_keys)

    def protect_phone(
        self,
        phone: str,
        *,
        table: str = "sms_message",
        column: str = "phone_enc",
    ) -> ProtectedPhone:
        """一次性生成逐号码持久化四元组。"""

        self._validate_phone(phone)
        version = self.active_version
        phone_hmac = self.phone_hmac(phone, version)
        encrypted = self.encrypt_bound_text(
            phone,
            EncryptionContext(
                domain="phone",
                table=table,
                column=column,
                object_id=phone_hmac,
            ),
        )
        return ProtectedPhone(
            phone_enc=encrypted.payload,
            phone_hmac=phone_hmac,
            phone_mask=self.mask_phone(phone),
            key_version=encrypted.key_version,
        )

    def decrypt_phone(
        self,
        payload: bytes,
        key_version: int,
        phone_hmac: str,
        *,
        table: str = "sms_message",
        column: str = "phone_enc",
        allow_legacy: bool = False,
    ) -> str:
        """仅在提供持久化 HMAC 与表字段上下文时解密手机号。"""

        if re.fullmatch(r"[0-9a-f]{64}", phone_hmac) is None:
            raise ValueError("phone_hmac is invalid")
        phone = self.decrypt_bound_text(
            payload,
            key_version,
            EncryptionContext(
                domain="phone",
                table=table,
                column=column,
                object_id=phone_hmac,
            ),
            allow_legacy=allow_legacy,
        )
        self._validate_phone(phone)
        if not hmac.compare_digest(self.phone_hmac(phone, key_version), phone_hmac):
            raise ValueError("phone ciphertext and HMAC mismatch")
        return phone


@lru_cache
def get_crypto_service() -> CryptoService:
    """从进程配置构造并缓存数据加密服务。"""

    return CryptoService.from_settings(get_settings())
