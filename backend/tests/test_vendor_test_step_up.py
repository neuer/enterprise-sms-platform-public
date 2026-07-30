from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from app.core.auth.jwt import JwtClaims

PASSWORD = "Current@Password123"
IP = "203.0.113.10"


def claims(**updates: object) -> JwtClaims:
    values: dict[str, object] = {
        "account_id": 8,
        "identity_id": 18,
        "provider_code": "local",
        "login_name": "admin",
        "display_name": "管理员",
        "dept": "平台部",
        "role": "admin",
        "security_version": 3,
        "jti": "jwt-session-7",
    }
    values.update(updates)
    return JwtClaims(**values)  # type: ignore[arg-type]


class FakeAuthFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[JwtClaims, str, str]] = []

    async def reauthenticate_current(
        self,
        current: JwtClaims,
        password: str,
        ip: str,
    ) -> None:
        self.calls.append((current, password, ip))


class FakeStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []
        self.eval_calls: list[tuple[str, int, tuple[Any, ...]]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.set_calls.append((key, value, ex))

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        self.eval_calls.append((script, numkeys, args))
        key, expected = str(args[0]), str(args[1])
        stored = self.values.pop(key, None)
        if stored is None:
            return 0
        return 1 if stored == expected else -1


@pytest.mark.asyncio
async def test_issue_reauthenticates_current_provider_and_stores_only_token_digest() -> None:
    from app.services.vendor_test_step_up import VendorTestStepUpService

    auth = FakeAuthFacade()
    store = FakeStore()
    service = VendorTestStepUpService(auth, store)
    current = claims(provider_code="ad", login_name="User01")

    token = await service.issue(
        claims=current,
        password=PASSWORD,
        ip=IP,
        operation="activate",
    )

    assert auth.calls == [(current, PASSWORD, IP)]
    assert len(store.set_calls) == 1
    key, serialized, ttl = store.set_calls[0]
    assert key == f"vendor-test:step-up:{hashlib.sha256(token.encode()).hexdigest()}"
    assert token not in key and token not in serialized
    assert PASSWORD not in key and PASSWORD not in serialized
    assert ttl == 300
    assert json.loads(serialized) == {
        "account_id": 8,
        "identity_id": 18,
        "ip": IP,
        "jti": "jwt-session-7",
        "login_name": "User01",
        "operation": "activate",
        "provider_code": "ad",
    }


@pytest.mark.asyncio
async def test_token_is_bound_to_account_jti_ip_and_operation_and_consumed_once() -> None:
    from app.services.vendor_test_step_up import StepUpExpired, VendorTestStepUpService

    store = FakeStore()
    service = VendorTestStepUpService(FakeAuthFacade(), store)
    current = claims()
    token = await service.issue(
        claims=current,
        password=PASSWORD,
        ip=IP,
        operation="activate",
    )

    await service.consume(token, current, IP, "activate")

    with pytest.raises(StepUpExpired):
        await service.consume(token, current, IP, "activate")


@pytest.mark.parametrize(
    ("changed_claims", "ip", "operation"),
    (
        ({"account_id": 9}, IP, "activate"),
        ({"identity_id": 19}, IP, "activate"),
        ({"provider_code": "ad"}, IP, "activate"),
        ({"login_name": "another-admin"}, IP, "activate"),
        ({"jti": "another-session"}, IP, "activate"),
        ({}, "203.0.113.11", "activate"),
        ({}, IP, "rotate_credentials"),
    ),
)
@pytest.mark.asyncio
async def test_wrong_binding_fails_closed_and_consumes_token(
    changed_claims: dict[str, object],
    ip: str,
    operation: str,
) -> None:
    from app.services.vendor_test_step_up import StepUpExpired, VendorTestStepUpService

    store = FakeStore()
    service = VendorTestStepUpService(FakeAuthFacade(), store)
    current = claims()
    token = await service.issue(
        claims=current,
        password=PASSWORD,
        ip=IP,
        operation="activate",
    )

    with pytest.raises(StepUpExpired):
        await service.consume(token, claims(**changed_claims), ip, operation)
    with pytest.raises(StepUpExpired):
        await service.consume(token, current, IP, "activate")


@pytest.mark.asyncio
async def test_arbitrary_operation_is_rejected_before_reauthentication_or_storage() -> None:
    from app.services.vendor_test_step_up import (
        InvalidStepUpOperation,
        VendorTestStepUpService,
    )

    auth = FakeAuthFacade()
    store = FakeStore()
    service = VendorTestStepUpService(auth, store)

    with pytest.raises(InvalidStepUpOperation):
        await service.issue(
            claims=claims(),
            password=PASSWORD,
            ip=IP,
            operation="arbitrary-shell",
        )

    assert not auth.calls and not store.set_calls


@pytest.mark.asyncio
async def test_reset_configuration_token_is_bound_and_consumed_once() -> None:
    from app.services.vendor_test_step_up import VendorTestStepUpService

    auth = FakeAuthFacade()
    store = FakeStore()
    service = VendorTestStepUpService(auth, store)
    current = claims()

    token = await service.issue(
        claims=current,
        password=PASSWORD,
        ip=IP,
        operation="reset_configuration",
    )
    await service.consume(token, current, IP, "reset_configuration")

    assert auth.calls == [(current, PASSWORD, IP)]
    with pytest.raises(PermissionError):
        await service.consume(token, current, IP, "reset_configuration")
