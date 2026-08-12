from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest

from app.core.auth.accounts import PlatformAccount
from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    ProviderDisabled,
    ProviderUnavailable,
    SessionStateUnavailable,
)
from app.core.auth.jwt import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    JwtClaims,
    JwtService,
)
from app.core.auth.ldap_real import LdapConfig, LdapPasswordProvider
from app.core.auth.mock import MockLdapProvider
from app.core.auth.providers import AuthProviderRegistry
from app.core.auth.roles import ExistingUser, RoleResolver
from app.core.auth.service import AccountLocked, AuthService, LoginGuard, RateLimited
from app.services.auth_provider import ProviderRecord, ProviderTestResult
from app.services.runtime_policy import RuntimePolicy

TAB_ID = "a" * 32


class FakeKeyValue:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any, *, ex: int) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def increment(self, key: str, *, window_s: int) -> int:
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        del script
        if numkeys == 3:
            revoked_jti, revoked_session, refresh_family, jti_ttl, session_ttl = args
            assert int(jti_ttl) > 0 and int(session_ttl) > 0
            self.values[str(revoked_jti)] = "1"
            self.values[str(revoked_session)] = "1"
            self.values.pop(str(refresh_family), None)
            return 1
        assert numkeys == 2
        key, revoked_session, expected, replacement, _ttl, session_ttl = args
        current = self.values.get(str(key))
        if current is None:
            assert int(session_ttl) > 0
            self.values[str(revoked_session)] = "1"
            self.values.pop(str(key), None)
            return 0
        if current != expected:
            assert int(session_ttl) > 0
            self.values[str(revoked_session)] = "1"
            self.values.pop(str(key), None)
            return -1
        self.values[str(key)] = replacement
        return 1


def access_claims(
    *,
    role: str = "operator",
    security_version: int = 1,
) -> JwtClaims:
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        display_name="开发操作员",
        dept="业务一部",
        role=role,  # type: ignore[arg-type]
        security_version=security_version,
    )


def account_projection(
    *,
    role: str = "operator",
    security_version: int = 1,
    account_enabled: bool = True,
    identity_enabled: bool = True,
    provider_enabled: bool = True,
) -> PlatformAccount:
    return PlatformAccount(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        normalized_login_name="operator01",
        display_name="开发操作员",
        dept="业务一部",
        role=role,  # type: ignore[arg-type]
        security_version=security_version,
        account_enabled=account_enabled,
        identity_enabled=identity_enabled,
        provider_enabled=provider_enabled,
    )


def provider_record(
    *,
    code: str,
    kind: str,
    enabled: bool = True,
) -> ProviderRecord:
    now = datetime(2026, 7, 16, 8, tzinfo=UTC)
    config: dict[str, object] = {} if kind == "local" else {"server": "ldaps://dc.example:636"}
    return ProviderRecord(
        id=1 if code == "local" else 2,
        code=code,
        name="本地账号" if code == "local" else "AD 账号",
        kind=kind,
        enabled=enabled,
        draft_config=config,
        active_config=config if enabled else None,
        draft_version=1,
        tested_version=1 if enabled and kind != "local" else None,
        active_version=1 if enabled and kind != "local" else None,
        last_tested_at=None,
        last_test_status=None,
        created_at=now,
        updated_at=now,
    )


class FakeProviderRepository:
    def __init__(self, *records: ProviderRecord) -> None:
        self.records = {record.code: record for record in records}

    async def get(self, code: str) -> ProviderRecord:
        return self.records[code]


class RecordingProviderKind:
    def __init__(self, provider_code: str, *, fails: bool = False) -> None:
        self.provider_code = provider_code
        self.fails = fails
        self.calls: list[tuple[str, str]] = []

    def auth_flow(self) -> str:
        return "password"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        return config

    async def test_config(self, config: dict[str, object]) -> ProviderTestResult:
        return ProviderTestResult(True, "OK")

    async def authenticate(
        self,
        record: ProviderRecord,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        self.calls.append((login_name, password))
        if self.fails:
            raise InvalidCredentials("uniform failure")
        return AuthenticatedIdentity(
            provider_code=record.code,
            login_name=login_name,
            external_subject=f"{record.code}:{login_name}",
            display_name=login_name,
            dept="研发部",
            groups=("CN=SMS-Viewers",),
        )


class CapacityUnavailableProviderKind(RecordingProviderKind):
    async def authenticate(
        self,
        record: ProviderRecord,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        self.calls.append((login_name, password))
        raise ProviderCapacityUnavailable("capacity unavailable")


@pytest.mark.asyncio
async def test_mock_backend_only_accepts_seed_users_and_injected_password() -> None:
    password = "in-memory-test-password"
    backend = MockLdapProvider(password)
    identity = await backend.authenticate("admin01", password)
    assert identity.provider_code == "ad"
    assert identity.external_subject == "mock:admin01"
    assert identity.display_name == "开发管理员"
    assert identity.dept == "平台技术部"
    assert identity.groups == ("mock:admin",)
    assert identity.development_role == "admin"

    with pytest.raises(InvalidCredentials):
        await backend.authenticate("admin01", "wrong")
    with pytest.raises(InvalidCredentials):
        await backend.authenticate("unknown", password)


@pytest.mark.asyncio
async def test_ldap_backend_searches_then_binds_user_with_ldap3_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.auth.ldap_real as ldap_module

    calls: list[dict[str, Any]] = []
    tls_calls: list[dict[str, Any]] = []

    class Attribute:
        def __init__(self, value: Any = None, values: list[str] | None = None) -> None:
            self.value = value
            self.values = values or []

    class Entry:
        entry_dn = "CN=测试用户,OU=Users,DC=xtc,DC=com"
        sAMAccountName = Attribute("user01")
        displayName = Attribute("测试用户")
        department = Attribute("研发部")
        objectGUID = Attribute("guid-user-01")
        memberOf = Attribute(values=["CN=SMS-Viewers,DC=xtc,DC=com"])

    class FakeConnection:
        def __init__(self, server: object, **kwargs: Any) -> None:
            calls.append(kwargs)
            self.entries = [Entry()]
            self.socket = object()

        def open(self) -> None:
            return None

        def bind(self) -> bool:
            return True

        def search(self, **kwargs: Any) -> bool:
            calls.append(kwargs)
            return True

        def unbind(self) -> None:
            return None

    def tls_factory(**kwargs: Any) -> object:
        tls_calls.append(kwargs)
        return object()

    def server_factory(url: str, **kwargs: Any) -> object:
        calls.append({"server_url": url, **kwargs})
        return object()

    monkeypatch.setattr(ldap_module, "Tls", tls_factory)
    monkeypatch.setattr(ldap_module, "Server", server_factory)
    monkeypatch.setattr(ldap_module, "Connection", FakeConnection)
    backend = LdapPasswordProvider(
        LdapConfig(
            provider_code="ad",
            server="ldaps://dc.example:636",
            base_dn="DC=xtc,DC=com",
            bind_dn="CN=svc,DC=xtc,DC=com",
            bind_password="service-secret",
            user_search_filter="(sAMAccountName={username})",
            username_attribute="sAMAccountName",
            display_name_attribute="displayName",
            dept_attribute="department",
            subject_attribute="objectGUID",
            group_attribute="memberOf",
            ca_certs_file="/etc/ssl/certs/ca-certificates.crt",
            connect_timeout_s=3,
            receive_timeout_s=4,
        )
    )

    identity = await backend.authenticate("user01", "user-password")

    assert identity == AuthenticatedIdentity(
        provider_code="ad",
        login_name="user01",
        external_subject="guid-user-01",
        display_name="测试用户",
        dept="研发部",
        groups=("CN=SMS-Viewers,DC=xtc,DC=com",),
    )
    assert tls_calls[0]["validate"] != 0
    assert tls_calls[0]["ca_certs_file"] == "/etc/ssl/certs/ca-certificates.crt"
    assert calls[0]["connect_timeout"] == 3
    assert calls[0]["allowed_referral_hosts"] == []
    assert calls[1]["receive_timeout"] == 4
    assert calls[1]["user"] == "CN=svc,DC=xtc,DC=com"
    assert calls[1]["auto_referrals"] is False
    assert calls[3]["user"] == Entry.entry_dn
    assert calls[3]["auto_referrals"] is False


@pytest.mark.asyncio
async def test_ldap_invalid_credentials_result_is_uniform_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ldap3.core.exceptions import LDAPInvalidCredentialsResult

    import app.core.auth.ldap_real as ldap_module

    class Attribute:
        value = "user01"
        values = ["CN=SMS-Viewers,DC=xtc,DC=com"]

    class Entry:
        entry_dn = "CN=user01,DC=xtc,DC=com"
        sAMAccountName = Attribute()
        displayName = Attribute()
        department = Attribute()
        objectGUID = Attribute()
        memberOf = Attribute()

    class FakeConnection:
        def __init__(self, server: object, **kwargs: Any) -> None:
            del server
            self.user = kwargs["user"]
            self.entries = [Entry()]
            self.socket = object()

        def open(self) -> None:
            return None

        def bind(self) -> bool:
            if self.user == Entry.entry_dn:
                raise LDAPInvalidCredentialsResult(result=49)
            return True

        def search(self, **kwargs: Any) -> bool:
            del kwargs
            return True

        def unbind(self) -> None:
            return None

    monkeypatch.setattr(ldap_module, "Tls", lambda **_: object())
    monkeypatch.setattr(ldap_module, "Server", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ldap_module, "Connection", FakeConnection)
    backend = LdapPasswordProvider(
        LdapConfig(
            provider_code="ad",
            server="ldaps://dc.example:636",
            base_dn="DC=xtc,DC=com",
            bind_dn="CN=svc,DC=xtc,DC=com",
            bind_password="service-secret",
            user_search_filter="(sAMAccountName={username})",
            username_attribute="sAMAccountName",
            display_name_attribute="displayName",
            dept_attribute="department",
            subject_attribute="objectGUID",
            group_attribute="memberOf",
            ca_certs_file="/etc/ssl/certs/ca-certificates.crt",
            connect_timeout_s=3,
            receive_timeout_s=4,
        )
    )

    with pytest.raises(InvalidCredentials, match="用户名或密码错误"):
        await backend.authenticate("user01", "wrong-password")


@pytest.mark.asyncio
async def test_ldap_connection_test_binds_and_performs_bounded_directory_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.auth.ldap_real as ldap_module

    searches: list[dict[str, Any]] = []

    class FakeConnection:
        def __init__(self, server: object, **kwargs: Any) -> None:
            del server, kwargs
            self.socket = object()

        def open(self) -> None:
            return None

        def bind(self) -> bool:
            return True

        def search(self, **kwargs: Any) -> bool:
            searches.append(kwargs)
            return True

        def unbind(self) -> None:
            return None

    monkeypatch.setattr(ldap_module, "Tls", lambda **_: object())
    monkeypatch.setattr(ldap_module, "Server", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ldap_module, "Connection", FakeConnection)
    backend = LdapPasswordProvider(
        LdapConfig(
            provider_code="ad",
            server="ldaps://dc.example:636",
            base_dn="DC=xtc,DC=com",
            bind_dn="CN=svc,DC=xtc,DC=com",
            bind_password="service-secret",
            user_search_filter="(sAMAccountName={username})",
            username_attribute="sAMAccountName",
            display_name_attribute="displayName",
            dept_attribute="department",
            subject_attribute="objectGUID",
            group_attribute="memberOf",
            ca_certs_file="/etc/ssl/certs/ca-certificates.crt",
            connect_timeout_s=3,
            receive_timeout_s=4,
        )
    )

    await backend.test_connection()

    assert searches == [
        {
            "search_base": "DC=xtc,DC=com",
            "search_filter": "(objectClass=*)",
            "search_scope": ldap_module.SUBTREE,
            "attributes": ["objectGUID"],
            "size_limit": 1,
        }
    ]


def test_ldap_receive_budget_fails_before_unbounded_parser_materialization() -> None:
    import app.core.auth.ldap_real as ldap_module

    class Socket:
        def recv(self, size: int, *_args: object) -> bytes:
            return b"x" * size

    bounded = ldap_module._ReceiveBudgetSocket(Socket(), limit=4)

    assert bounded.recv(3) == b"xxx"
    with pytest.raises(OSError, match="byte budget"):
        bounded.recv(3)


def test_ldap_group_attribute_has_count_and_aggregate_budgets() -> None:
    import app.core.auth.ldap_real as ldap_module

    attribute = SimpleNamespace(values=["group"] * (ldap_module.LDAP_MAX_GROUPS + 1))
    entry = SimpleNamespace(memberOf=attribute)

    with pytest.raises(ProviderUnavailable, match="组属性超出限制"):
        ldap_module._attribute_values(entry, "memberOf")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), Exception("backpressure")])
async def test_ldap_capacity_failure_uses_isolated_pool_and_aligned_deadline(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    import app.core.auth.ldap_real as ldap_module

    if not isinstance(failure, TimeoutError):
        failure = ldap_module.ExecutorBackpressure("full")
    observed: dict[str, object] = {}

    async def fail_bounded(
        function: object,
        *args: object,
        timeout_s: float,
        pool: str,
        **kwargs: object,
    ) -> object:
        del function, args, kwargs
        observed.update(timeout_s=timeout_s, pool=pool)
        raise failure

    monkeypatch.setattr(ldap_module, "run_bounded", fail_bounded)
    provider = LdapPasswordProvider(
        LdapConfig(
            provider_code="ad",
            server="ldaps://dc.example:636",
            base_dn="DC=example,DC=com",
            bind_dn="CN=svc,DC=example,DC=com",
            bind_password="service-secret",
            user_search_filter="(uid={username})",
            username_attribute="uid",
            display_name_attribute="displayName",
            dept_attribute="department",
            subject_attribute="entryUUID",
            group_attribute="memberOf",
            ca_certs_file="/ca.pem",
            connect_timeout_s=3,
            receive_timeout_s=4,
        )
    )

    with pytest.raises(ProviderCapacityUnavailable):
        await provider.authenticate("user01", "password")

    assert observed == {"timeout_s": 19, "pool": "ldap"}


@pytest.mark.asyncio
async def test_registry_never_falls_back_to_another_provider() -> None:
    local = RecordingProviderKind("local", fails=True)
    ad = RecordingProviderKind("ad", fails=False)
    registry = AuthProviderRegistry(
        FakeProviderRepository(
            provider_record(code="local", kind="local"),
            provider_record(code="ad", kind="ldap"),
        ),
        {"local": local, "ldap": ad},
    )

    with pytest.raises(InvalidCredentials):
        await registry.authenticate("local", "admin", "bad")

    assert local.calls == [("admin", "bad")]
    assert ad.calls == []


@pytest.mark.asyncio
async def test_registry_supports_local_and_ad_simultaneously_and_rejects_disabled() -> None:
    local = RecordingProviderKind("local")
    ad = RecordingProviderKind("ad")
    repository = FakeProviderRepository(
        provider_record(code="local", kind="local"),
        provider_record(code="ad", kind="ldap"),
    )
    registry = AuthProviderRegistry(repository, {"local": local, "ldap": ad})

    assert (await registry.authenticate("local", "admin", "pw")).provider_code == "local"
    assert (await registry.authenticate("ad", "admin", "pw")).provider_code == "ad"

    repository.records["ad"] = provider_record(code="ad", kind="ldap", enabled=False)
    with pytest.raises(ProviderDisabled):
        await registry.authenticate("ad", "admin", "pw")
    assert ad.calls == [("admin", "pw")]


@pytest.mark.asyncio
async def test_shared_guard_locks_account_after_five_failures() -> None:
    store = FakeKeyValue()
    local = RecordingProviderKind("local", fails=True)
    ad = RecordingProviderKind("ad", fails=True)
    registry = AuthProviderRegistry(
        FakeProviderRepository(
            provider_record(code="local", kind="local"),
            provider_record(code="ad", kind="ldap"),
        ),
        {"local": local, "ldap": ad},
    )
    service = AuthService(registry, LoginGuard(store))

    for _ in range(4):
        with pytest.raises(InvalidCredentials):
            await service.authenticate("local", " User01 ", "bad", "10.0.0.1")
    with pytest.raises(AccountLocked):
        await service.authenticate("ad", "user01", "bad", "10.0.0.1")
    with pytest.raises(AccountLocked):
        await service.authenticate("local", "user01", "correct", "10.0.0.2")

    assert list(store.values).count("auth:lock:user:user01") == 1


@pytest.mark.asyncio
async def test_provider_success_cannot_clear_username_failures_before_account_binding() -> None:
    store = FakeKeyValue()
    guard = LoginGuard(store)
    await guard.record_failure("shared-user", "10.0.0.1")
    provider = RecordingProviderKind("ad")
    service = AuthService(
        AuthProviderRegistry(
            FakeProviderRepository(provider_record(code="ad", kind="ldap")),
            {"ldap": provider},
        ),
        guard,
    )

    identity = await service.authenticate("ad", "shared-user", "pw", "10.0.0.2")

    assert identity.provider_code == "ad"
    assert store.values["auth:fail:user:shared-user"] == 1
    await service.record_bound_success("shared-user")
    assert "auth:fail:user:shared-user" not in store.values


@pytest.mark.asyncio
async def test_shared_guard_bans_ip_after_twenty_failures() -> None:
    store = FakeKeyValue()
    guard = LoginGuard(store)

    for index in range(19):
        await guard.record_failure(f"user{index}", "10.0.0.1")
    with pytest.raises(RateLimited):
        await guard.record_failure("user20", "10.0.0.1")
    passing = RecordingProviderKind("local")
    registry = AuthProviderRegistry(
        FakeProviderRepository(provider_record(code="local", kind="local")),
        {"local": passing},
    )
    with pytest.raises(RateLimited):
        await AuthService(registry, guard).authenticate(
            "local",
            "good",
            "correct",
            "10.0.0.1",
        )


@pytest.mark.asyncio
async def test_provider_capacity_failure_is_admitted_before_scheduling_without_user_lock(
) -> None:
    store = FakeKeyValue()
    overloaded = CapacityUnavailableProviderKind("ad")
    local = RecordingProviderKind("local")
    registry = AuthProviderRegistry(
        FakeProviderRepository(
            provider_record(code="local", kind="local"),
            provider_record(code="ad", kind="ldap"),
        ),
        {"local": local, "ldap": overloaded},
    )
    service = AuthService(registry, LoginGuard(store))

    with pytest.raises(ProviderCapacityUnavailable):
        await service.authenticate("ad", "target-admin", "pw", "10.0.0.1")
    with pytest.raises(ProviderCapacityUnavailable):
        await service.authenticate("ad", "another-user", "pw", "10.0.0.1")
    local_identity = await service.authenticate(
        "local",
        "local-admin",
        "pw",
        "10.0.0.1",
    )

    assert overloaded.calls == [("target-admin", "pw")]
    assert local_identity.provider_code == "local"
    assert store.values["auth:capacity-fail:ip:10.0.0.1"] == 1
    assert "auth:lock:user:target-admin" not in store.values
    assert "auth:lock:user:another-user" not in store.values


@pytest.mark.asyncio
async def test_repeated_provider_capacity_failures_advance_ip_ban() -> None:
    store = FakeKeyValue()
    overloaded = CapacityUnavailableProviderKind("ad")
    registry = AuthProviderRegistry(
        FakeProviderRepository(provider_record(code="ad", kind="ldap")),
        {"ldap": overloaded},
    )

    async def load_policy() -> RuntimePolicy:
        return RuntimePolicy.from_mapping(
            {
                "login_ip_fail_limit": "2",
                "login_ip_ban_minutes": "3",
            }
        )

    service = AuthService(registry, LoginGuard(store, policy_loader=load_policy))
    with pytest.raises(ProviderCapacityUnavailable):
        await service.authenticate("ad", "first", "pw", "10.0.0.2")
    store.values.pop("auth:capacity:provider:ad")
    with pytest.raises(RateLimited):
        await service.authenticate("ad", "second", "pw", "10.0.0.2")

    assert store.values["auth:ban:ip:10.0.0.2"] == "1"
    assert "auth:lock:user:first" not in store.values
    assert "auth:lock:user:second" not in store.values


@pytest.mark.asyncio
async def test_shared_guard_loads_runtime_thresholds_for_each_login_boundary() -> None:
    store = FakeKeyValue()
    values = {
        "login_fail_limit": "2",
        "login_lock_minutes": "3",
        "login_ip_fail_limit": "2",
        "login_ip_ban_minutes": "4",
    }

    async def load_policy() -> RuntimePolicy:
        return RuntimePolicy.from_mapping(values)

    guard = LoginGuard(store, policy_loader=load_policy)
    await guard.record_failure("first", "10.0.0.1")
    with pytest.raises(RateLimited):
        await guard.record_failure("second", "10.0.0.1")
    assert store.values["auth:ban:ip:10.0.0.1"] == "1"

    values["login_ip_fail_limit"] = "3"
    await guard.record_failure("third", "10.0.0.2")
    await guard.record_failure("fourth", "10.0.0.2")


def test_role_resolver_honors_override_and_mapping_without_hidden_bootstrap() -> None:
    resolver = RoleResolver()
    identity = AuthenticatedIdentity(
        "ad",
        "mapped",
        "guid-mapped",
        "映射用户",
        "研发部",
        ("viewers", "approvers"),
    )
    decision = resolver.resolve(
        identity,
        None,
        {"viewers": "viewer", "approvers": "approver"},
    )
    assert decision.role == "approver"
    overridden = ExistingUser(role="operator", role_override=True)
    assert resolver.resolve(identity, overridden, {"approvers": "admin"}).role == "operator"

    with pytest.raises(InvalidCredentials):
        resolver.resolve(
            AuthenticatedIdentity("ad", "unmapped", "guid-unmapped", "未映射", "研发部", ()),
            None,
            {},
        )

    with pytest.raises(InvalidCredentials):
        resolver.resolve(
            AuthenticatedIdentity(
                "ad",
                "directory-controlled",
                "guid-directory-controlled",
                "目录用户",
                "研发部",
                ("mock:admin",),
            ),
            None,
            {},
        )

    mock_identity = AuthenticatedIdentity(
        "ad",
        "admin01",
        "mock:admin01",
        "开发管理员",
        "平台技术部",
        ("mock:admin",),
        development_role="admin",
    )
    assert resolver.resolve(mock_identity, None, {}).role == "admin"


@pytest.mark.asyncio
async def test_jwt_jti_logout_and_force_user_revoke() -> None:
    store = FakeKeyValue()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        clock=lambda: now,
        ttl=timedelta(minutes=15),
    )
    token = service.issue(access_claims())
    claims = await service.verify(token)
    assert claims.account_id == 8
    assert claims.identity_id == 18
    assert claims.provider_code == "local"
    assert claims.login_name == "operator01"
    assert claims.jti

    await service.revoke_token(token)
    with pytest.raises(InvalidCredentials):
        await service.verify(token)

    second = service.issue(access_claims())
    await service.revoke_user(8)
    with pytest.raises(InvalidCredentials):
        await service.verify(second)


@pytest.mark.asyncio
async def test_logout_revokes_every_access_token_in_the_same_session() -> None:
    store = FakeKeyValue()
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
    )
    first = await service.issue_pair(access_claims(), TAB_ID)
    second = await service.rotate_refresh(first.refresh_token, TAB_ID)

    await service.revoke_token(second.token)

    with pytest.raises(InvalidCredentials):
        await service.verify(first.token)
    with pytest.raises(InvalidCredentials):
        await service.verify(second.token)


@pytest.mark.asyncio
async def test_refresh_family_is_bound_to_issuing_browser_tab() -> None:
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
    )
    pair = await service.issue_pair(access_claims(), TAB_ID)

    with pytest.raises(InvalidCredentials, match="标签页"):
        await service.rotate_refresh(pair.refresh_token, "d" * 32)

    rotated = await service.rotate_refresh(pair.refresh_token, TAB_ID)
    assert (await service.verify(rotated.token)).account_id == 8


@pytest.mark.asyncio
async def test_logout_by_refresh_token_revokes_family_when_access_is_expired() -> None:
    store = FakeKeyValue()
    moments = [datetime(2026, 7, 11, 8, 0, tzinfo=UTC)]
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        clock=lambda: moments[0],
        ttl=timedelta(minutes=15),
    )
    pair = await service.issue_pair(access_claims(), TAB_ID)
    moments[0] += timedelta(minutes=16)

    claims = await service.revoke_refresh_token(pair.refresh_token)

    assert claims.account_id == 8
    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_force_logout_does_not_revoke_a_new_login_in_the_same_second() -> None:
    store = FakeKeyValue()
    moments = [datetime(2026, 7, 11, 8, 0, tzinfo=UTC)]
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        clock=lambda: moments[0],
        ttl=timedelta(minutes=15),
    )
    old_token = service.issue(access_claims())
    await service.revoke_user(8)
    with pytest.raises(InvalidCredentials):
        await service.verify(old_token)

    moments[0] += timedelta(milliseconds=10)
    new_token = service.issue(access_claims())

    assert (await service.verify(new_token)).username == "operator01"


@pytest.mark.asyncio
async def test_force_user_revoke_also_blocks_refresh_rotation() -> None:
    store = FakeKeyValue()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        clock=lambda: now,
    )
    pair = await service.issue_pair(access_claims(), TAB_ID)

    await service.revoke_user(8)

    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
@pytest.mark.authorization
async def test_jwt_rejects_authoritative_security_context_mismatch_without_race() -> None:
    store = FakeKeyValue()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    current = [account_projection()]

    async def load_security_session(account_id: int, identity_id: int) -> PlatformAccount:
        assert (account_id, identity_id) == (8, 18)
        return current[0]

    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        clock=lambda: now,
        security_session_loader=load_security_session,
    )
    old_token = service.issue(access_claims(security_version=1))
    assert (await service.verify(old_token)).security_version == 1

    current[0] = account_projection(role="approver", security_version=2)
    with pytest.raises(InvalidCredentials):
        await service.verify(old_token)

    new_token = service.issue(access_claims(role="approver", security_version=2))
    assert (await service.verify(new_token)).role == "approver"


@pytest.mark.parametrize(
    "projection",
    (
        account_projection(account_enabled=False),
        account_projection(identity_enabled=False),
        account_projection(provider_enabled=False),
        PlatformAccount(
            8,
            18,
            "ad",
            "operator01",
            "operator01",
            "开发操作员",
            "业务一部",
            "operator",
            1,
            True,
            True,
        ),
        PlatformAccount(
            8,
            18,
            "local",
            "operator01",
            "operator01",
            "开发操作员",
            "调整后部门",
            "operator",
            1,
            True,
            True,
        ),
    ),
)
@pytest.mark.asyncio
async def test_jwt_rejects_disabled_or_changed_authoritative_projection(
    projection: PlatformAccount,
) -> None:
    async def load_security_session(
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount:
        assert (account_id, identity_id) == (8, 18)
        return projection

    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        security_session_loader=load_security_session,
    )

    with pytest.raises(InvalidCredentials):
        await service.verify(service.issue(access_claims()))


@pytest.mark.asyncio
@pytest.mark.authorization
async def test_refresh_replay_revokes_family_with_authoritative_projection() -> None:
    store = FakeKeyValue()

    async def load_security_session(
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount:
        assert (account_id, identity_id) == (8, 18)
        return account_projection()

    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        security_session_loader=load_security_session,
    )
    first = await service.issue_pair(access_claims(), TAB_ID)
    second = await service.rotate_refresh(first.refresh_token, TAB_ID)
    assert (await service.verify(second.token)).account_id == 8

    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(first.refresh_token, TAB_ID)
    with pytest.raises(InvalidCredentials):
        await service.verify(second.token)
    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(second.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_refresh_family_has_an_absolute_seven_day_lifetime() -> None:
    moments = [datetime(2026, 7, 11, 8, 0, tzinfo=UTC)]

    async def load_security_session(
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount:
        assert (account_id, identity_id) == (8, 18)
        return account_projection()

    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
        clock=lambda: moments[0],
        security_session_loader=load_security_session,
    )
    first = await service.issue_pair(access_claims(), TAB_ID)
    first_payload = jwt.decode(
        first.refresh_token,
        service.secret,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_aud": False},
    )
    moments[0] += timedelta(days=2)
    second = await service.rotate_refresh(first.refresh_token, TAB_ID)
    second_payload = jwt.decode(
        second.refresh_token,
        service.secret,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_aud": False},
    )

    assert second.refresh_expires_in == 5 * 24 * 60 * 60
    assert second_payload["family_exp"] == first_payload["family_exp"]
    assert second_payload["exp"] == first_payload["exp"]


@pytest.mark.asyncio
async def test_old_refresh_token_replay_immediately_destroys_family() -> None:
    store = FakeKeyValue()
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
    )
    first = await service.issue_pair(access_claims(), TAB_ID)
    second = await service.rotate_refresh(first.refresh_token, TAB_ID)

    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(first.refresh_token, TAB_ID)
    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(second.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_refresh_replay_revocation_failure_is_fail_closed() -> None:
    class RevocationUnavailable(FakeKeyValue):
        calls = 0

        async def eval(self, script: str, numkeys: int, *args: Any) -> int:
            if numkeys == 2:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("redis unavailable")
            return await super().eval(script, numkeys, *args)

    store = RevocationUnavailable()
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
    )
    first = await service.issue_pair(access_claims(), TAB_ID)
    await service.rotate_refresh(first.refresh_token, TAB_ID)

    with pytest.raises(SessionStateUnavailable):
        await service.rotate_refresh(first.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_session_state_failures_are_fail_closed() -> None:
    class UnavailableStore(FakeKeyValue):
        async def get(self, key: str) -> Any:
            del key
            raise RuntimeError("redis unavailable")

    async def unavailable_projection(
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount:
        del account_id, identity_id
        raise RuntimeError("database unavailable")

    token = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
    ).issue(access_claims())
    with pytest.raises(SessionStateUnavailable):
        await JwtService(
            "a-jwt-secret-that-is-long-enough-for-hs256-tests",
            UnavailableStore(),
        ).verify(token)

    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
        security_session_loader=unavailable_projection,
    )
    with pytest.raises(SessionStateUnavailable):
        await service.verify(service.issue(access_claims()))

    corrupted = FakeKeyValue()
    corrupted.values["auth:jwt:account-revoked:8"] = "not-a-timestamp"
    corrupted_service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        corrupted,
    )
    with pytest.raises(SessionStateUnavailable):
        await corrupted_service.verify(corrupted_service.issue(access_claims()))


@pytest.mark.asyncio
async def test_jwt_rejects_old_username_subject_and_password_change_tokens() -> None:
    store = FakeKeyValue()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    secret = "a-jwt-secret-that-is-long-enough-for-hs256-tests"
    service = JwtService(secret, store, clock=lambda: now)
    legacy = jwt.encode(
        {
            "sub": "operator01",
            "display_name": "开发操作员",
            "dept": "业务一部",
            "role": "operator",
            "auth_version": 1,
            "jti": "legacy-token",
            "iat": now.timestamp(),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidCredentials):
        await service.verify(legacy)

    change_token = service.issue_password_change(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
    )
    with pytest.raises(InvalidCredentials):
        await service.verify(change_token)


def _keyring(active: int, keys: dict[int, str]) -> str:
    return json.dumps(
        {
            "active_version": active,
            "keys": {
                str(version): base64.b64encode(key.encode("utf-8")).decode("ascii")
                for version, key in keys.items()
            },
        }
    )


def test_jwt_new_tokens_carry_kid_issuer_and_audience() -> None:
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
    )
    token = service.issue(access_claims())

    header = jwt.get_unverified_header(token)
    assert header["kid"] == "1"
    payload = jwt.decode(
        token,
        service.secret,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_aud": False},
    )
    assert payload["iss"] == JWT_ISSUER
    assert payload["aud"] == JWT_AUDIENCE


def test_jwt_unknown_kid_and_wrong_audience_fail_closed() -> None:
    service = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
    )
    token = service.issue(access_claims())
    unknown = jwt.encode(
        jwt.decode(
            token,
            service.secret,
            algorithms=["HS256"],
            options={"verify_exp": False, "verify_aud": False},
        ),
        service.secret,
        algorithm="HS256",
        headers={"kid": "99"},
    )
    with pytest.raises(InvalidCredentials):
        service._decode(unknown)

    wrong_aud = jwt.encode(
        {
            **jwt.decode(
                token,
                service.secret,
                algorithms=["HS256"],
                options={"verify_exp": False, "verify_aud": False},
            ),
            "aud": "other-service",
        },
        service.secret,
        algorithm="HS256",
        headers={"kid": "1"},
    )
    with pytest.raises(InvalidCredentials):
        service._decode(wrong_aud)


@pytest.mark.asyncio
async def test_jwt_keyring_rotation_keeps_old_verification_keys() -> None:
    old_secret = _keyring(1, {"1": "o" * 32})
    rotated_secret = _keyring(
        2,
        {
            "1": "o" * 32,
            "2": "n" * 32,
        },
    )
    old_service = JwtService(old_secret, FakeKeyValue())
    rotated_service = JwtService(rotated_secret, FakeKeyValue())

    old_token = old_service.issue(access_claims())
    assert (await rotated_service.verify(old_token)).account_id == 8
    assert jwt.get_unverified_header(rotated_service.issue(access_claims()))["kid"] == "2"


@pytest.mark.asyncio
async def test_jwt_legacy_token_rejected_when_compat_window_closed() -> None:
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    secret = "a-jwt-secret-that-is-long-enough-for-hs256-tests"
    strict = JwtService(
        secret,
        FakeKeyValue(),
        clock=lambda: now,
        accept_legacy=False,
    )
    legacy = jwt.encode(
        {
            "sub": "8",
            "identity_id": 18,
            "provider_code": "local",
            "login_name": "operator01",
            "display_name": "操作员",
            "dept": "平台部",
            "role": "operator",
            "security_version": 1,
            "token_type": "access",
            "sid": "session",
            "jti": "legacy-token",
            "iat": now.timestamp(),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidCredentials):
        await strict.verify(legacy)


@pytest.mark.asyncio
async def test_legacy_jwt_still_requires_issuer_and_audience() -> None:
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    secret = "a-jwt-secret-that-is-long-enough-for-hs256-tests"
    compatible = JwtService(
        secret,
        FakeKeyValue(),
        clock=lambda: now,
        accept_legacy=True,
    )
    base = {
        "sub": "8",
        "identity_id": 18,
        "provider_code": "local",
        "login_name": "operator01",
        "display_name": "操作员",
        "dept": "平台部",
        "role": "operator",
        "security_version": 1,
        "token_type": "access",
        "sid": "session",
        "jti": "legacy-token",
        "iat": now.timestamp(),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    valid_legacy = jwt.encode(base, secret, algorithm="HS256")
    assert (await compatible.verify(valid_legacy)).account_id == 8

    wrong_aud = jwt.encode({**base, "aud": "other-service"}, secret, algorithm="HS256")
    with pytest.raises(InvalidCredentials):
        await compatible.verify(wrong_aud)

    wrong_iss = jwt.encode({**base, "iss": "other-issuer"}, secret, algorithm="HS256")
    with pytest.raises(InvalidCredentials):
        await compatible.verify(wrong_iss)
