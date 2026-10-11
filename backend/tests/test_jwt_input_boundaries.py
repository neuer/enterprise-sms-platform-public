"""Bound malformed JWT input before library parsing; retain normal auth semantics."""

from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
import pytest

from app.core.auth.backends import InvalidCredentials
from app.core.auth.jwt import JWT_AUDIENCE, JWT_ISSUER, JwtService, parse_jwt_keyring
from app.core.auth.service import AsyncKeyValue


@pytest.fixture
def service() -> JwtService:
    return JwtService(secrets.token_urlsafe(32), cast(AsyncKeyValue, object()))


@pytest.mark.parametrize("secret", ["", "x", "x" * 31, " " * 40, "\t" * 40, "密" * 10])
@pytest.mark.parametrize("boundary", ["parser", "service"])
def test_raw_key_requires_nonblank_32_bytes(secret: str, boundary: str) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes") as caught:
        if boundary == "parser":
            parse_jwt_keyring(secret)
        else:
            JwtService(secret, cast(AsyncKeyValue, object()))
    assert str(caught.value) == "JWT key must contain at least 32 bytes of key material"


@pytest.mark.parametrize("secret", ["x" * 32, "x" * 64, "密" * 11, " " + "x" * 32 + " "])
def test_raw_key_preserves_exact_material_and_v1_tokens(secret: str) -> None:
    ring = parse_jwt_keyring(secret)
    assert ring.active_version == 1
    assert ring.keys == {1: secret.encode("utf-8")}
    issuer = JwtService(secret, cast(AsyncKeyValue, object()))
    verifier = JwtService(secret, cast(AsyncKeyValue, object()))
    token = issuer.issue_password_change(
        account_id=1, identity_id=2, provider_code="local", login_name="test-user"
    )
    assert jwt.get_unverified_header(token)["kid"] == "1"
    assert verifier._decode(token)["identity_id"] == 2


@pytest.mark.parametrize("size", [0, 1, 31, 32, 64])
def test_json_keyring_uses_same_byte_boundary(size: int) -> None:
    key = b"k" * size
    secret = json.dumps({"active_version": 1, "keys": {"1": base64.b64encode(key).decode()}})
    if size < 32:
        with pytest.raises(ValueError, match="at least 32 bytes"):
            parse_jwt_keyring(secret)
    else:
        assert parse_jwt_keyring(secret).keys == {1: key}


def token_with_header(header: object) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    return encoded + ".e30.AA"


@pytest.mark.authorization
@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "....",
        "a" * 8193,
        "a" * 1025 + ".e30.AA",
        "非ASCII.e30.AA",
        "!!!!.e30.AA",
        token_with_header([]),
        token_with_header({"alg": "none", "kid": "1"}),
        token_with_header({"alg": "HS256", "kid": []}),
    ],
)
def test_invalid_inputs_are_credentials_errors(service: JwtService, token: str) -> None:
    with pytest.raises(InvalidCredentials):
        service._decode(token)


def test_oversize_input_does_not_reach_parser(
    service: JwtService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_: str) -> dict[str, object]:
        raise AssertionError("untrusted oversized token reached parser")

    monkeypatch.setattr(jwt, "get_unverified_header", unexpected)
    with pytest.raises(InvalidCredentials):
        service._decode("x" * 8193)


def test_nested_header_is_rejected_without_uncaught_recursion(service: JwtService) -> None:
    raw = '{"alg":"HS256","extra":' + "[" * 1100 + "0" + "]" * 1100 + "}"
    token = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode() + ".e30.AA"
    with pytest.raises(InvalidCredentials):
        service._decode(token)


@pytest.mark.parametrize("entry", ["get_unverified_header", "decode"])
def test_library_recursion_is_converted_at_decode_boundary(
    service: JwtService,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    token = service.issue_password_change(
        account_id=1, identity_id=2, provider_code="local", login_name="test-user"
    )

    def fail(*args: object, **kwargs: object) -> dict[str, object]:
        raise RecursionError("untrusted parser detail")

    monkeypatch.setattr(jwt, entry, fail)
    with pytest.raises(InvalidCredentials) as caught:
        service._decode(token)
    assert "untrusted parser detail" not in str(caught.value)


def test_valid_token_still_decodes(service: JwtService) -> None:
    token = service.issue_password_change(
        account_id=1, identity_id=2, provider_code="local", login_name="test-user"
    )
    assert service._decode(token)["identity_id"] == 2


@pytest.mark.parametrize("change", ["signature", "issuer", "audience", "expiry"])
def test_signature_and_claim_failures_remain_rejected(service: JwtService, change: str) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": "1",
        "token_type": "password_change",
        "jti": "test-only",
        "iat": now.timestamp(),
        "exp": int((now + timedelta(minutes=1)).timestamp()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    if change == "issuer":
        payload["iss"] = "other"
    elif change == "audience":
        payload["aud"] = "other"
    elif change == "expiry":
        payload["exp"] = int((now - timedelta(seconds=10)).timestamp())
    key = secrets.token_urlsafe(32) if change == "signature" else service._signing_key()
    token = jwt.encode(payload, key, algorithm="HS256", headers={"kid": "1"})
    with pytest.raises(InvalidCredentials):
        service._decode(token)
