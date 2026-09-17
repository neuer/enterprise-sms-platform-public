"""管理员单次授权的用途、主体及副作用前校验。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from app.core.audit import AuditEvent
from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.jwt import JwtClaims
from app.core.errors import ApiError
from app.services.admin_step_up import AdminStepUpService, admin_intent

CLAIMS = JwtClaims(7, 17, "local", "synthetic", "synthetic", "synthetic", "admin", 2, "jti")
INTENT = admin_intent("user_role_change", "77", {"role": "admin", "role_override": True})


class Store:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail = False

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        assert ex == 300
        self.values[key] = value
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)

    async def eval(self, _: str, numkeys: int, *args: Any) -> int:
        assert numkeys == 1
        if self.fail:
            raise OSError("synthetic store failure")
        value = self.values.pop(args[0], None)
        return 0 if value is None else 1 if value == args[1] else -1


class Auth:
    def __init__(self) -> None:
        self.current = CLAIMS
        self.invalid = False
        self.reauth: list[tuple[str, str]] = []

    async def reauthenticate_current(self, claims: JwtClaims, password: str, ip: str) -> None:
        self.reauth.append((claims.provider_code, ip))
        if password != "synthetic-valid":
            raise ApiError(401, "STEP_UP_REQUIRED", "synthetic failure", None)

    async def verify(self, _: str) -> JwtClaims:
        if self.invalid:
            raise ApiError(401, "UNAUTHORIZED", "synthetic revoked session", None)
        return self.current


def setup() -> tuple[AdminStepUpService, Store, Auth, list[AuditEvent]]:
    store, auth, events = Store(), Auth(), []

    async def audit(event: AuditEvent) -> None:
        events.append(event)

    return AdminStepUpService(auth, store, audit), store, auth, events


@pytest.mark.asyncio
async def test_only_one_concurrent_consumer_and_no_secret_in_stored_fact_or_audit() -> None:
    service, store, _, events = setup()
    token = await service.issue(
        claims=CLAIMS, password="synthetic-valid", ip="synthetic", intent=INTENT
    )
    assert all(token not in key for key in store.values)
    assert "synthetic-valid" not in json.dumps(store.values)
    results = await asyncio.gather(
        *[
            service.consume(
                token, claims=CLAIMS, access_token="access", ip="synthetic", intent=INTENT
            )
            for _ in range(8)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, AdminAuthorization) for result in results) == 1
    assert sum(isinstance(result, ApiError) for result in results) == 7
    assert all(
        "synthetic-valid" not in str(event.after) and token not in str(event.after)
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    ["account", "identity", "provider", "jti", "version", "ip", "target", "role", "operation"],
)
async def test_binding_mismatch_burns_the_token(variant: str) -> None:
    service, _, _, _ = setup()
    token = await service.issue(
        claims=CLAIMS, password="synthetic-valid", ip="synthetic", intent=INTENT
    )
    changes = {
        "account": {"account_id": 8},
        "identity": {"identity_id": 18},
        "provider": {"provider_code": "ad"},
        "jti": {"jti": "other"},
        "version": {"security_version": 3},
    }
    claims = replace(CLAIMS, **changes.get(variant, {}))
    intent = INTENT
    if variant == "target":
        intent = replace(INTENT, target_id="88")
    if variant == "role":
        intent = admin_intent("user_role_change", "77", {"role": "operator", "role_override": True})
    if variant == "operation":
        intent = admin_intent("user_password_reset", "77", {"action": "reset"})
    with pytest.raises(ApiError):
        await service.consume(
            token,
            claims=claims,
            access_token="access",
            ip="other" if variant == "ip" else "synthetic",
            intent=intent,
        )
    with pytest.raises(ApiError):
        await service.consume(
            token, claims=CLAIMS, access_token="access", ip="synthetic", intent=INTENT
        )


@pytest.mark.asyncio
async def test_failed_password_and_audit_never_return_a_token() -> None:
    service, store, _, events = setup()
    with pytest.raises(ApiError):
        await service.issue(claims=CLAIMS, password="wrong", ip="synthetic", intent=INTENT)
    assert not store.values
    assert events[-1].after == {
        "operation": "user_role_change",
        "result": "reauthentication_failed",
    }

    async def fail_audit(_: AuditEvent) -> None:
        raise OSError("synthetic audit failure")

    service.audit_sink = fail_audit
    with pytest.raises(ApiError):
        await service.issue(
            claims=CLAIMS, password="synthetic-valid", ip="synthetic", intent=INTENT
        )
    assert not store.values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["revoked", "demoted", "version", "audit_failure", "store_failure", "missing"]
)
async def test_final_authority_or_storage_failure_prevents_authorization(changed: str) -> None:
    service, store, auth, _ = setup()
    token = await service.issue(
        claims=CLAIMS, password="synthetic-valid", ip="synthetic", intent=INTENT
    )
    if changed == "revoked":
        auth.invalid = True
    if changed == "demoted":
        auth.current = replace(CLAIMS, role="viewer")
    if changed == "version":
        auth.current = replace(CLAIMS, security_version=3)
    if changed == "missing":
        store.values.clear()
    if changed == "store_failure":
        store.fail = True
    if changed == "audit_failure":

        async def fail_audit(_: AuditEvent) -> None:
            raise OSError("synthetic audit failure")

        service.audit_sink = fail_audit
    with pytest.raises((ApiError, OSError)):
        await service.consume(
            token, claims=CLAIMS, access_token="access", ip="synthetic", intent=INTENT
        )
    if changed != "store_failure":
        assert not store.values


def test_intent_rejects_client_supplied_digest_or_credentials() -> None:
    for key in ["password", "temporary_password", "request_fingerprint"]:
        with pytest.raises(ApiError):
            admin_intent("user_password_reset", "77", {"action": "reset", key: "forbidden"})


def test_mapping_revision_and_configuration_are_bound_independently() -> None:
    parameters = {
        "expected_revision": "a" * 64,
        "mappings": [
            {"external_group": "synthetic", "role": "admin", "dept": "synthetic"},
        ],
    }
    original = admin_intent("provider_role_mapping_change", "ad", parameters)
    changed_revision = admin_intent(
        "provider_role_mapping_change", "ad", {**parameters, "expected_revision": "b" * 64}
    )
    changed_mapping = admin_intent(
        "provider_role_mapping_change", "ad", {**parameters, "mappings": []}
    )
    assert (
        len({original.fingerprint, changed_revision.fingerprint, changed_mapping.fingerprint}) == 3
    )
