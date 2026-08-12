from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.auth.providers import LdapProviderKind
from app.services.auth_provider import (
    AuthProviderService,
    DuplicateRoleMapping,
    ExternalRoleMapping,
    ImmutableProvider,
    InvalidProviderConfig,
    ProviderRecord,
    ProviderSummary,
    ProviderTestResult,
    UntestedProviderConfig,
    validate_ldap_allowed,
)

ADMIN = "admin"
IP = "10.0.0.8"
NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)


def valid_ad_config() -> dict[str, object]:
    return {
        "server": "ldaps://dc01.example.com:636",
        "base_dn": "DC=example,DC=com",
        "bind_dn": "CN=sms-reader,OU=Service,DC=example,DC=com",
        "user_search_filter": "(sAMAccountName={username})",
        "username_attribute": "sAMAccountName",
        "display_name_attribute": "displayName",
        "dept_attribute": "department",
        "subject_attribute": "objectGUID",
        "group_attribute": "memberOf",
        "connect_timeout_s": 5.0,
        "receive_timeout_s": 10.0,
    }


def test_ldap_provider_kind_rejects_target_outside_deployment_allowlist() -> None:
    kind = LdapProviderKind(
        SimpleNamespace(ldap_allowed_host_set=frozenset({"dc01.example.com:636"}))
    )
    assert kind.validate_config(valid_ad_config())["server"] == "ldaps://dc01.example.com:636"
    with pytest.raises(InvalidProviderConfig, match="部署允许列表"):
        kind.validate_config(
            {**valid_ad_config(), "server": "ldaps://evil.example.com:636"}
        )


def test_ldap_allowed_list_empty_fails_closed() -> None:
    with pytest.raises(InvalidProviderConfig, match="部署允许列表"):
        validate_ldap_allowed("ldaps://dc01.example.com:636", frozenset())


def provider(
    *,
    code: str = "ad",
    kind: str = "ldap",
    enabled: bool = False,
    draft_config: dict[str, object] | None = None,
    active_config: dict[str, object] | None = None,
    draft_version: int = 1,
    tested_version: int | None = None,
    active_version: int | None = None,
) -> ProviderRecord:
    return ProviderRecord(
        id=2 if code == "ad" else 1,
        code=code,
        name="AD 账号" if code == "ad" else "本地账号",
        kind=kind,
        enabled=enabled,
        draft_config=draft_config or {},
        active_config=active_config,
        draft_version=draft_version,
        tested_version=tested_version,
        active_version=active_version,
        last_tested_at=None,
        last_test_status=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeProviderRepository:
    def __init__(self, records: tuple[ProviderRecord, ...]) -> None:
        self.records = {item.code: item for item in records}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_enabled(self) -> tuple[ProviderRecord, ...]:
        return tuple(item for item in self.records.values() if item.enabled)

    async def get(self, code: str) -> ProviderRecord:
        return self.records[code]

    async def save_draft(
        self,
        code: str,
        config: dict[str, object],
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        current = self.records[code]
        saved = replace(
            current,
            draft_config=config,
            draft_version=current.draft_version + 1,
            tested_version=None,
            last_tested_at=None,
            last_test_status=None,
        )
        self.records[code] = saved
        self.calls.append(("save", {"actor": actor, "ip": ip}))
        return saved

    async def record_test(
        self,
        code: str,
        version: int,
        result: ProviderTestResult,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        current = self.records[code]
        saved = replace(
            current,
            tested_version=version if result.success else None,
            last_tested_at=NOW,
            last_test_status="success" if result.success else "failed",
        )
        self.records[code] = saved
        self.calls.append(
            (
                "test",
                {
                    "version": version,
                    "result_code": result.result_code,
                    "actor": actor,
                    "ip": ip,
                },
            )
        )
        return saved

    async def activate(
        self,
        code: str,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        current = self.records[code]
        if current.tested_version != current.draft_version:
            raise UntestedProviderConfig("当前草稿尚未通过测试")
        saved = replace(
            current,
            enabled=True,
            active_config=current.draft_config,
            active_version=current.draft_version,
        )
        self.records[code] = saved
        self.calls.append(("activate", {"actor": actor, "ip": ip}))
        return saved

    async def disable(
        self,
        code: str,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        current = self.records[code]
        saved = replace(current, enabled=False)
        self.records[code] = saved
        self.calls.append(("disable", {"actor": actor, "ip": ip}))
        return saved

    async def list_role_mappings(self, code: str) -> tuple[ExternalRoleMapping, ...]:
        del code
        return (ExternalRoleMapping("CN=SMS-Admins", "admin", "平台部"),)

    async def replace_role_mappings(
        self,
        code: str,
        mappings: tuple[ExternalRoleMapping, ...],
        *,
        actor: str,
        ip: str,
    ) -> tuple[ExternalRoleMapping, ...]:
        self.calls.append(
            (
                "mappings",
                {"code": code, "mappings": mappings, "actor": actor, "ip": ip},
            )
        )
        return mappings


class FakeProviderRegistry:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.tested: list[tuple[str, dict[str, object]]] = []

    def auth_flow(self, kind: str) -> str:
        assert kind in {"local", "ldap"}
        return "password"

    def validate_config(self, kind: str, config: dict[str, object]) -> dict[str, object]:
        assert kind in {"local", "ldap"}
        return config

    async def test_config(
        self,
        kind: str,
        config: dict[str, object],
    ) -> ProviderTestResult:
        self.tested.append((kind, config))
        return ProviderTestResult(self.succeeds, "OK" if self.succeeds else "LDAP_BIND_FAILED")


@pytest.mark.asyncio
async def test_enabled_provider_list_contains_only_public_flow_metadata() -> None:
    repository = FakeProviderRepository(
        (
            provider(code="local", kind="local", enabled=True),
            provider(enabled=False, draft_config=valid_ad_config()),
        )
    )
    service = AuthProviderService(repository, FakeProviderRegistry())

    assert await service.list_enabled() == (ProviderSummary("local", "本地账号", "password"),)


@pytest.mark.asyncio
async def test_local_provider_is_immutable() -> None:
    repository = FakeProviderRepository((provider(code="local", kind="local", enabled=True),))
    service = AuthProviderService(repository, FakeProviderRegistry())

    with pytest.raises(ImmutableProvider):
        await service.save_draft("local", {}, actor=ADMIN, ip=IP)
    with pytest.raises(ImmutableProvider):
        await service.disable("local", actor=ADMIN, ip=IP)

    assert repository.calls == []


@pytest.mark.asyncio
async def test_only_current_tested_draft_can_activate() -> None:
    repository = FakeProviderRepository((provider(),))
    registry = FakeProviderRegistry(succeeds=True)
    service = AuthProviderService(repository, registry)

    first = await service.save_draft("ad", valid_ad_config(), actor=ADMIN, ip=IP)
    tested = await service.test_draft("ad", actor=ADMIN, ip=IP)
    second = await service.save_draft(
        "ad",
        {**valid_ad_config(), "base_dn": "DC=new,DC=example,DC=com"},
        actor=ADMIN,
        ip=IP,
    )

    assert tested.success and first.draft_version == 2
    assert second.draft_version == 3 and second.tested_version is None
    with pytest.raises(UntestedProviderConfig):
        await service.activate("ad", actor=ADMIN, ip=IP)


@pytest.mark.asyncio
async def test_failed_test_never_authorizes_activation() -> None:
    repository = FakeProviderRepository((provider(),))
    service = AuthProviderService(repository, FakeProviderRegistry(succeeds=False))

    await service.save_draft("ad", valid_ad_config(), actor=ADMIN, ip=IP)
    result = await service.test_draft("ad", actor=ADMIN, ip=IP)

    assert result == ProviderTestResult(False, "LDAP_BIND_FAILED")
    assert repository.records["ad"].tested_version is None
    with pytest.raises(UntestedProviderConfig):
        await service.activate("ad", actor=ADMIN, ip=IP)


@pytest.mark.asyncio
async def test_successful_current_test_activates_and_disable_preserves_config() -> None:
    repository = FakeProviderRepository((provider(),))
    service = AuthProviderService(repository, FakeProviderRegistry())

    draft = await service.save_draft("ad", valid_ad_config(), actor=ADMIN, ip=IP)
    await service.test_draft("ad", actor=ADMIN, ip=IP)
    active = await service.activate("ad", actor=ADMIN, ip=IP)
    disabled = await service.disable("ad", actor=ADMIN, ip=IP)

    assert active.enabled
    assert active.active_version == draft.draft_version
    assert active.active_config == draft.draft_config
    assert not disabled.enabled
    assert disabled.active_config == active.active_config
    assert disabled.active_version == active.active_version


@pytest.mark.asyncio
async def test_role_mapping_replace_is_provider_scoped_validated_and_local_is_immutable() -> None:
    repository = FakeProviderRepository(
        (
            provider(draft_config=valid_ad_config()),
            provider(code="local", kind="local", enabled=True),
        )
    )
    service = AuthProviderService(repository, FakeProviderRegistry())
    mappings = (
        ExternalRoleMapping(" CN=SMS-Admins ", "admin", " 平台部 "),
        ExternalRoleMapping("CN=SMS-Operators", "operator", "业务一部"),
    )

    replaced = await service.replace_role_mappings(
        "ad",
        mappings,
        actor=ADMIN,
        ip=IP,
    )

    assert replaced[0].external_group == "CN=SMS-Admins"
    assert replaced[0].dept == "平台部"
    assert await service.list_role_mappings("ad") == (
        ExternalRoleMapping("CN=SMS-Admins", "admin", "平台部"),
    )
    with pytest.raises(DuplicateRoleMapping):
        await service.replace_role_mappings(
            "ad",
            (
                ExternalRoleMapping("CN=Same", "admin", "平台部"),
                ExternalRoleMapping(" CN=Same ", "viewer", "业务一部"),
            ),
            actor=ADMIN,
            ip=IP,
        )

    with pytest.raises(InvalidProviderConfig, match="授权部门"):
        await service.replace_role_mappings(
            "ad",
            (ExternalRoleMapping("CN=Missing-Dept", "viewer"),),
            actor=ADMIN,
            ip=IP,
        )
    with pytest.raises(ImmutableProvider):
        await service.replace_role_mappings(
            "local",
            (),
            actor=ADMIN,
            ip=IP,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"server": "ldap://dc01.example.com:389"}, "LDAPS"),
        ({"server": "ldaps://reader:secret@dc01.example.com"}, "凭据"),
        ({"server": "ldaps://dc01.example.com/path"}, "origin"),
        ({"base_dn": ""}, "Base DN"),
        ({"bind_dn": ""}, "Bind DN"),
        ({"user_search_filter": "(uid=someone)"}, "{username}"),
        ({"user_search_filter": "(|(uid={username})(mail={username}))"}, "{username}"),
        ({"username_attribute": "uid)(objectClass=*"}, "属性"),
        ({"connect_timeout_s": 0}, "超时"),
        ({"receive_timeout_s": 31}, "超时"),
        ({"bind_password": "must-never-enter-database"}, "未知配置"),
    ),
)
@pytest.mark.asyncio
async def test_ad_draft_requires_strict_non_sensitive_ldaps_config(
    updates: dict[str, object],
    message: str,
) -> None:
    repository = FakeProviderRepository((provider(),))
    service = AuthProviderService(repository, FakeProviderRegistry())

    with pytest.raises(InvalidProviderConfig, match=message):
        await service.save_draft(
            "ad",
            {**valid_ad_config(), **updates},
            actor=ADMIN,
            ip=IP,
        )

    assert repository.calls == []
