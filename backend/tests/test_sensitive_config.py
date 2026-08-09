from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.services.sensitive_config import AlertCredentialCipher


def test_alert_credential_is_write_only_for_public_key_holder() -> None:
    private = X25519PrivateKey.from_private_bytes(b"a" * 32)
    public_only = AlertCredentialCipher(public_key=private.public_key())
    callback_only = AlertCredentialCipher(private_key=private)
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"

    sealed = public_only.seal(webhook)

    assert sealed.startswith("sealed:v1:")
    assert webhook not in sealed
    assert callback_only.open(sealed) == webhook
    with pytest.raises(RuntimeError, match="private key"):
        public_only.open(sealed)


def test_alert_credential_rejects_wrong_private_key_and_tampering() -> None:
    private = X25519PrivateKey.from_private_bytes(b"b" * 32)
    other = X25519PrivateKey.from_private_bytes(b"c" * 32)
    sealed = AlertCredentialCipher(public_key=private.public_key()).seal("credential")

    with pytest.raises(ValueError, match="invalid"):
        AlertCredentialCipher(private_key=other).open(sealed)
    with pytest.raises(ValueError, match="invalid"):
        AlertCredentialCipher(private_key=private).open(sealed[:-1] + "A")
