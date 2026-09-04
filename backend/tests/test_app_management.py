from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from typing import Any

import pytest

from app.services.app_management import (
    AppCreate,
    AppManagementService,
    AppUpdate,
    CallbackUrlValidator,
    InvalidAppConfig,
)
from app.services.crypto import CryptoService


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create(self, **values: Any) -> int:
        self.calls.append(("create", values))
        return 17

    async def list(self) -> list[dict[str, Any]]:
        return []

    async def get(self, app_id: int) -> dict[str, Any] | None:
        return {"id": app_id, "name": "app-existing"}

    async def update(self, app_id: int, **values: Any) -> dict[str, Any]:
        self.calls.append(("update", {"app_id": app_id, **values}))
        return {"id": app_id, **values}

    async def rotate_key(self, app_id: int, **values: Any) -> None:
        self.calls.append(("rotate_key", {"app_id": app_id, **values}))

    async def revoke_old_key(self, app_id: int, actor: str, ip: str) -> None:
        self.calls.append(("revoke_old_key", {"app_id": app_id, "actor": actor, "ip": ip}))

    async def rotate_callback_secret(self, app_id: int, **values: Any) -> None:
        self.calls.append(("rotate_callback_secret", {"app_id": app_id, **values}))

    async def disable(self, app_id: int, actor: str, ip: str) -> None:
        self.calls.append(("disable", {"app_id": app_id, "actor": actor, "ip": ip}))


def crypto() -> CryptoService:
    key = base64.b64encode(b"a" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def validator(*addresses: str) -> CallbackUrlValidator:
    return CallbackUrlValidator(
        "10.0.0.0/8,192.168.0.0/16",
        resolver=lambda _: list(addresses),
        allow_http=True,
    )


@pytest.mark.parametrize(
    "cidrs",
    ("0.0.0.0/0", "::/0", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16"),
)
def test_callback_validator_cannot_be_constructed_with_unsafe_allowlist(
    cidrs: str,
) -> None:
    with pytest.raises(InvalidAppConfig, match="私网"):
        CallbackUrlValidator(cidrs, resolver=lambda _: ["100.64.0.1"])


@pytest.mark.asyncio
async def test_production_callback_validator_rejects_http_but_dev_can_opt_in() -> None:
    resolver = lambda _hostname: ["10.1.2.3"]  # noqa: E731
    production = CallbackUrlValidator("10.0.0.0/8", resolver=resolver)
    development = CallbackUrlValidator(
        "10.0.0.0/8",
        resolver=resolver,
        allow_http=True,
    )
    with pytest.raises(InvalidAppConfig, match="HTTPS"):
        await production.validate_for_save("http://callback.internal/hook")
    assert (
        await development.validate_for_save("http://callback.internal/hook")
        == "http://callback.internal/hook"
    )


@pytest.mark.asyncio
async def test_callback_validator_enforces_deployment_cidr_and_port_ceiling() -> None:
    with pytest.raises(InvalidAppConfig, match="部署允许"):
        CallbackUrlValidator(
            "10.0.0.0/8",
            deployment_allow_cidrs=(ip_network("10.20.0.0/16"),),
            deployment_allow_ports=(443,),
            resolver=lambda _: ["10.20.1.7"],
        )

    validator = CallbackUrlValidator(
        "10.20.0.0/16",
        deployment_allow_cidrs=(ip_network("10.20.0.0/16"),),
        deployment_allow_ports=(443,),
        resolver=lambda _: ["10.20.1.7"],
    )
    with pytest.raises(InvalidAppConfig, match="端口超出"):
        await validator.validate_for_save("https://callback.internal:8443/hook")
    assert await validator.validate_for_save("https://callback.internal/hook")


@pytest.mark.asyncio
async def test_callback_validator_empty_deployment_rejects_before_dns() -> None:
    resolved_hosts: list[str] = []

    def resolver(hostname: str) -> list[str]:
        resolved_hosts.append(hostname)
        return ["10.20.1.7"]

    validator = CallbackUrlValidator(
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        deployment_allow_cidrs=(),
        deployment_allow_ports=(443,),
        resolver=resolver,
    )

    with pytest.raises(InvalidAppConfig, match="出站未启用"):
        await validator.validate_for_save("https://callback.internal/hook")

    assert resolved_hosts == []


@pytest.mark.asyncio
async def test_app_without_callback_works_when_callback_egress_is_disabled() -> None:
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        CallbackUrlValidator(
            "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
            deployment_allow_cidrs=(),
            deployment_allow_ports=(443,),
        ),
        secret_generator=lambda: "api-key-plain-once",
    )

    assert await service.list() == []
    assert await service.create(
        AppCreate(name="app-no-callback", dept="研发部"),
        actor="admin01",
        ip="10.0.0.8",
    ) == {
        "id": 17,
        "api_key": "api-key-plain-once",
        "callback_secret": None,
    }


@pytest.mark.asyncio
async def test_create_returns_secrets_once_but_repository_only_gets_hash_and_ciphertext() -> None:
    repo = FakeRepository()
    secrets = iter(["api-key-plain-once", "callback-secret-plain-once"])
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.2.3.4"),
        secret_generator=lambda: next(secrets),
    )

    result = await service.create(
        AppCreate(name="app-new", dept="研发部", callback_url="http://callback.internal/hook"),
        actor="admin01",
        ip="10.0.0.8",
    )

    assert result == {
        "id": 17,
        "api_key": "api-key-plain-once",
        "callback_secret": "callback-secret-plain-once",
    }
    operation, stored = repo.calls[0]
    assert operation == "create"
    assert stored["api_key_prefix"] == "api-key-"
    assert len(stored["api_key_hash"]) == 64
    assert "api_key_hash_version" in stored
    assert b"callback-secret-plain-once" not in stored["callback_secret_enc"]
    assert int.from_bytes(stored["callback_secret_enc"][:2], "big") == 1
    assert "api-key-plain-once" not in str(stored)
    assert "callback-secret-plain-once" not in str(stored)


@pytest.mark.asyncio
async def test_rotate_key_sets_previous_expiry_and_revoke_is_explicit() -> None:
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.1.1.1"),
        secret_generator=lambda: "new-api-key-plain-value",
        clock=lambda: now,
        key_grace=timedelta(hours=72),
    )

    result = await service.rotate_key(9, actor="admin01", ip="10.0.0.8")
    assert result["api_key"] == "new-api-key-plain-value"
    assert result["old_key_expires_at"] == now + timedelta(hours=72)
    assert repo.calls[0][1]["old_key_expires_at"] == now + timedelta(hours=72)
    assert "api_key_hash_version" in repo.calls[0][1]
    assert "new-api-key-plain-value" not in str(repo.calls[0][1])

    await service.revoke_old_key(9, actor="admin01", ip="10.0.0.8")
    assert repo.calls[1][0] == "revoke_old_key"


@pytest.mark.asyncio
async def test_update_and_outbound_validation_reject_public_or_rebound_address() -> None:
    repo = FakeRepository()
    public = AppManagementService(repo, crypto(), validator("100.64.0.1"))
    with pytest.raises(InvalidAppConfig, match="CIDR"):
        await public.update(
            1,
            AppUpdate(dept="研发部", callback_url="https://callback.example/hook"),
            actor="admin01",
            ip="10.0.0.8",
        )

    rebound = validator("10.1.1.1", "100.64.0.1")
    with pytest.raises(InvalidAppConfig):
        await rebound.validate_for_outbound("https://callback.example/hook")


@pytest.mark.asyncio
async def test_callback_secret_rotation_only_persists_packed_ciphertext() -> None:
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.1.1.1"),
        secret_generator=lambda: "rotated-callback-secret",
    )
    result = await service.rotate_callback_secret(3, actor="admin01", ip="10.0.0.8")

    assert result == {"callback_secret": "rotated-callback-secret"}
    stored = repo.calls[0][1]
    assert b"rotated-callback-secret" not in stored["callback_secret_enc"]


def test_app_create_defaults_to_notice_only() -> None:
    created = AppCreate(name="app-default", dept="研发部")
    assert created.allowed_categories == frozenset({"notice"})
    assert created.allow_market_api_bulk is False
    assert created.recipient_limit_per_min == 10_000


@pytest.mark.asyncio
async def test_production_rejects_unlimited_quota_and_empty_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProductionSettings:
        environment = "production"

    monkeypatch.setattr("app.settings.get_settings", lambda: ProductionSettings())
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.1.1.1"),
        secret_generator=lambda: "api-key-plain-once",
        clock=lambda: now,
    )
    with pytest.raises(InvalidAppConfig, match="非零日配额"):
        await service.create(
            AppCreate(name="prod-open", dept="研发部", allowed_ips=("10.0.0.0/8",)),
            actor="admin01",
            ip="10.0.0.8",
        )
    with pytest.raises(InvalidAppConfig, match="来源 IP"):
        await service.create(
            AppCreate(name="prod-open", dept="研发部", daily_quota=1000),
            actor="admin01",
            ip="10.0.0.8",
        )
    result = await service.create(
        AppCreate(
            name="prod-exempt",
            dept="研发部",
            daily_quota=0,
            allowed_ips=(),
            unlimited_quota_exempt_until=now + timedelta(days=7),
            ip_allowlist_exempt_until=now + timedelta(days=7),
            admission_exempt_note="联调窗口",
        ),
        actor="admin01",
        ip="10.0.0.8",
    )
    assert result["id"] == 17
    stored = repo.calls[0][1]
    assert stored["admission_exempt_note"] == "联调窗口"
    assert stored["allow_market_api_bulk"] is False


@pytest.mark.asyncio
async def test_allowed_ips_are_normalized_deduped_and_sorted_on_create() -> None:
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.1.1.1"),
        secret_generator=lambda: "api-key-plain-once",
    )

    await service.create(
        AppCreate(
            name="app-ip",
            dept="研发部",
            allowed_ips=(
                "203.0.113.7",
                "203.0.113.7/32",
                "10.0.0.0/8",
                "2001:db8::1",
            ),
        ),
        actor="admin01",
        ip="10.0.0.8",
    )

    stored = repo.calls[0][1]
    assert stored["allowed_ips"] == (
        "10.0.0.0/8",
        "2001:db8::1/128",
        "203.0.113.7/32",
    )


@pytest.mark.asyncio
async def test_allowed_ips_reject_invalid_entries_and_exceed_bounds() -> None:
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.1.1.1"),
        secret_generator=lambda: "api-key-plain-once",
    )

    for invalid in (
        ("10.0.0.0/33",),
        ("example.com",),
        ("",),
        ("x" * 65,),
        (123,),  # type: ignore[arg-type]
    ):
        with pytest.raises(InvalidAppConfig, match="IP 白名单"):
            await service.create(
                AppCreate(name="app-ip", dept="研发部", allowed_ips=invalid),
                actor="admin01",
                ip="10.0.0.8",
            )

    too_many = tuple(f"10.0.{index}.0/24" for index in range(51))
    with pytest.raises(InvalidAppConfig, match="最多 50"):
        await service.create(
            AppCreate(name="app-ip", dept="研发部", allowed_ips=too_many),
            actor="admin01",
            ip="10.0.0.8",
        )


@pytest.mark.asyncio
async def test_update_requires_callback_secret_before_callback_url() -> None:
    repo = FakeRepository()
    service = AppManagementService(
        repo,
        crypto(),
        validator("10.1.1.1"),
        secret_generator=lambda: "api-key-plain-once",
    )

    with pytest.raises(InvalidAppConfig, match="callback secret"):
        await service.update(
            1,
            AppUpdate(
                dept="研发部",
                callback_url="http://callback.internal/hook",
            ),
            actor="admin01",
            ip="10.0.0.8",
        )
