"""企业微信凭据的 write-only 配置与 callback-only 解密信封。"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.settings import read_secret_file

WECOM_WEBHOOK_KEY = "alert_wecom_webhook"
SENSITIVE_CONFIG_PREFIX = "sealed:v1:"
_AAD = b"sms-platform:sys_config:alert_wecom_webhook:v1"
_HKDF_INFO = b"sms-platform:alert-credential-envelope:v1"
_EPHEMERAL_PUBLIC_BYTES = 32
_NONCE_BYTES = 12


def _decode_key_file(path: Path, *, label: str) -> bytes:
    try:
        raw = base64.b64decode(read_secret_file(path), validate=True)
    except (binascii.Error, ValueError) as error:
        raise RuntimeError(f"{label} is invalid") from error
    if len(raw) != 32:
        raise RuntimeError(f"{label} is invalid")
    return raw


class AlertCredentialCipher:
    """API 只持公钥封装；仅 callback worker 持私钥解封。"""

    def __init__(
        self,
        *,
        public_key: X25519PublicKey | None = None,
        private_key: X25519PrivateKey | None = None,
    ) -> None:
        if public_key is None and private_key is None:
            raise ValueError("alert credential key is required")
        self.public_key = public_key
        self.private_key = private_key

    @classmethod
    def from_public_file(cls, path: Path) -> AlertCredentialCipher:
        return cls(
            public_key=X25519PublicKey.from_public_bytes(
                _decode_key_file(path, label="alert credential public key")
            )
        )

    @classmethod
    def from_private_file(cls, path: Path) -> AlertCredentialCipher:
        return cls(
            private_key=X25519PrivateKey.from_private_bytes(
                _decode_key_file(path, label="alert credential private key")
            )
        )

    @staticmethod
    def _derive(shared_secret: bytes, ephemeral_public: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=ephemeral_public,
            info=_HKDF_INFO,
        ).derive(shared_secret)

    def seal(self, value: str) -> str:
        """使用 callback 公钥封装，API 无法反向恢复明文。"""

        if not value:
            return ""
        if self.public_key is None:
            raise RuntimeError("alert credential public key is unavailable")
        ephemeral = X25519PrivateKey.generate()
        ephemeral_public = ephemeral.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        key = self._derive(ephemeral.exchange(self.public_key), ephemeral_public)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), _AAD)
        packed = ephemeral_public + nonce + ciphertext
        return SENSITIVE_CONFIG_PREFIX + base64.b64encode(packed).decode("ascii")

    def open(self, value: str) -> str:
        """仅 callback 私钥可解封；旧格式与篡改值一律失败关闭。"""

        if not value:
            return ""
        if self.private_key is None:
            raise RuntimeError("alert credential private key is unavailable")
        if not value.startswith(SENSITIVE_CONFIG_PREFIX):
            raise ValueError("legacy alert credential envelope is forbidden")
        try:
            packed = base64.b64decode(
                value[len(SENSITIVE_CONFIG_PREFIX) :], validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise ValueError("alert credential envelope is invalid") from error
        minimum = _EPHEMERAL_PUBLIC_BYTES + _NONCE_BYTES + 16
        if len(packed) < minimum:
            raise ValueError("alert credential envelope is invalid")
        ephemeral_public = packed[:_EPHEMERAL_PUBLIC_BYTES]
        nonce_start = _EPHEMERAL_PUBLIC_BYTES
        nonce = packed[nonce_start : nonce_start + _NONCE_BYTES]
        ciphertext = packed[nonce_start + _NONCE_BYTES :]
        try:
            peer = X25519PublicKey.from_public_bytes(ephemeral_public)
            key = self._derive(self.private_key.exchange(peer), ephemeral_public)
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise ValueError("alert credential envelope is invalid") from error


def encrypt_wecom_webhook(value: str, cipher: AlertCredentialCipher) -> str:
    return cipher.seal(value)


def decrypt_wecom_webhook(value: str, cipher: AlertCredentialCipher) -> str:
    return cipher.open(value)
