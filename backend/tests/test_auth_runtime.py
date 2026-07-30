from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.core.auth.accounts import LocalAccountRecord, PlatformAccount
from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    ProviderDisabled,
    ProviderUnavailable,
)
from app.core.auth.jwt import JwtClaims, JwtService
from app.core.auth.runtime import (
    AuthFacade,
    LoginSuccess,
    PasswordChangeRequired,
)
from app.core.auth.service import AccountLocked
from app.core.errors import ApiError

IP = "10.0.0.8"
SECRET = "a-jwt-secret-that-is-long-enough-for-hs256-tests"


def account(*, must_change_password: bool) -> PlatformAccount:
    return PlatformAccount(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="admin",
        normalized_login_name="admin",
        display_name="管理员",
        dept="平台部",
        role="admin",
        security_version=3,
        account_enabled=True,
        identity_enabled=True,
        must_change_password=must_change_password,
    )


class FakeKeyValue:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    async def get(self, key: str) -> object | None:
        return self.values.get(key)

    async def set(self, key: str, value: object, *, ex: int) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def increment(self, key: str, *, window_s: int) -> int:
        del window_s
        current = self.values.get(key, 0)
        value = (current if isinstance(current, int) else 0) + 1
        self.values[key] = value
        return value

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        del script
        assert numkeys == 1
        key, expected, replacement, _ttl = args
        current = self.values.get(str(key))
        if current is None:
            return 0
        if current != expected:
            self.values.pop(str(key), None)
            return -1
        self.values[str(key)] = replacement
        return 1


class FakeAuthService:
    def __init__(self, identity: AuthenticatedIdentity | Exception) -> None:
        self.identity = identity
        self.calls: list[tuple[str, str, str, str]] = []

    async def authenticate(
        self,
        provider_code: str,
        login_name: str,
        password: str,
        ip: str,
    ) -> AuthenticatedIdentity:
        self.calls.append((provider_code, login_name, password, ip))
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity


class FakeUserRepository:
    def __init__(self, value: PlatformAccount) -> None:
        self.value = value
        self.changed: list[dict[str, object]] = []
        self.password_change_tokens: dict[str, dict[str, object]] = {}

    async def resolve_identity(
        self,
        identity: AuthenticatedIdentity,
        ip: str,
    ) -> PlatformAccount:
        del identity, ip
        return self.value

    async def find_local_account(
        self,
        normalized_login_name: str,
    ) -> LocalAccountRecord | None:
        if normalized_login_name != self.value.normalized_login_name:
            return None
        return LocalAccountRecord(self.value, "encoded-current")

    async def find_local_account_by_id(self, account_id: int) -> LocalAccountRecord | None:
        if account_id != self.value.account_id:
            return None
        return LocalAccountRecord(self.value, "encoded-current")

    async def create_password_change_token(
        self,
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
        security_version: int,
        expires_at: datetime,
    ) -> None:
        self.password_change_tokens[token_hash] = {
            "account_id": account_id,
            "identity_id": identity_id,
            "provider_code": provider_code,
            "login_name": login_name,
            "security_version": security_version,
            "expires_at": expires_at,
            "status": "available",
        }

    async def consume_password_change_and_update(
        self,
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
        password_hash: str,
        actor: str,
        ip: str,
    ) -> None:
        token = self.password_change_tokens.get(token_hash)
        if token is None or token["status"] != "available":
            raise InvalidCredentials("used")
        if (
            token["account_id"] != account_id
            or token["identity_id"] != identity_id
            or token["provider_code"] != provider_code
            or token["login_name"] != login_name
            or token["security_version"] != self.value.security_version
        ):
            raise InvalidCredentials("context mismatch")
        token["status"] = "consumed"
        await self.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash=password_hash,
            actor=actor,
            ip=ip,
        )

    async def change_local_password(
        self,
        *,
        account_id: int,
        identity_id: int,
        password_hash: str,
        actor: str,
        ip: str,
    ) -> None:
        self.changed.append(
            {
                "account_id": account_id,
                "identity_id": identity_id,
                "password_hash": password_hash,
                "actor": actor,
                "ip": ip,
            }
        )
        self.value = replace(
            self.value,
            security_version=self.value.security_version + 1,
            must_change_password=False,
        )

    async def load_security_session(
        self,
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount:
        assert account_id == self.value.account_id
        assert identity_id == self.value.identity_id
        return self.value

    async def invalidate_sessions(
        self,
        actor: PlatformAccount,
        account_id: int,
        ip: str,
    ) -> None:
        del actor, account_id, ip

    async def audit_logout(self, actor: PlatformAccount, ip: str) -> None:
        del actor, ip


class FakeHasher:
    def __init__(self) -> None:
        self.hashed: list[str] = []
        self.verified: list[tuple[str | None, str]] = []

    def hash(self, password: str) -> str:
        self.hashed.append(password)
        return f"encoded:{password}"

    def verify_or_dummy(self, encoded: str | None, password: str) -> bool:
        self.verified.append((encoded, password))
        return encoded == "encoded-current" and password == "Current@Password123"


def facade(
    *,
    must_change_password: bool,
) -> tuple[AuthFacade, FakeUserRepository, JwtService, FakeHasher]:
    value = account(must_change_password=must_change_password)
    identity = AuthenticatedIdentity(
        provider_code="local",
        login_name="admin",
        external_subject="local:admin",
        display_name="管理员",
        dept="平台部",
        groups=(),
        account=value,
    )
    users = FakeUserRepository(value)
    store = FakeKeyValue()
    tokens = JwtService(
        SECRET,
        store,
        clock=lambda: datetime(2026, 7, 16, 8, tzinfo=UTC),
        security_session_loader=users.load_security_session,
    )
    hasher = FakeHasher()
    return AuthFacade(FakeAuthService(identity), users, tokens, hasher), users, tokens, hasher


@pytest.mark.asyncio
async def test_temporary_password_login_requires_one_time_initial_change() -> None:
    service, users, tokens, hasher = facade(must_change_password=True)

    result = await service.login("local", "admin", "Temporary@123", IP)

    assert isinstance(result, PasswordChangeRequired)
    assert result.next_action == "change_password"
    assert result.token is None
    assert result.expires_in == 600
    digest = tokens.password_change_digest(result.change_token)
    assert users.password_change_tokens[digest]["status"] == "available"
    with pytest.raises(InvalidCredentials):
        await tokens.verify(result.change_token)

    await service.change_initial_password(result.change_token, "New@Password123", IP)

    assert hasher.hashed == ["New@Password123"]
    assert users.changed[0]["password_hash"] == "encoded:New@Password123"
    assert users.password_change_tokens[digest]["status"] == "consumed"
    with pytest.raises(ApiError) as reused:
        await service.change_initial_password(
            result.change_token,
            "Another@Password123",
            IP,
        )
    assert reused.value.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_normal_login_issues_account_based_access_token() -> None:
    service, _, tokens, _ = facade(must_change_password=False)

    result = await service.login("local", "Admin", "Valid@Password123", IP)

    assert isinstance(result, LoginSuccess)
    claims = await tokens.verify(result.token)
    assert claims.account_id == 8
    assert claims.identity_id == 18
    assert claims.provider_code == "local"
    assert claims.login_name == "admin"
    assert result.expires_in == 900
    assert result.refresh_expires_in == 604800
    refreshed = await service.refresh(result.refresh_token)
    assert refreshed.refresh_token != result.refresh_token
    assert (await tokens.verify(refreshed.token)).account_id == 8


@pytest.mark.asyncio
async def test_daily_password_change_checks_current_password_and_revokes_sessions() -> None:
    service, users, tokens, hasher = facade(must_change_password=False)
    login = await service.login("local", "admin", "Valid@Password123", IP)
    assert isinstance(login, LoginSuccess)

    await service.change_password(
        login.token,
        "Current@Password123",
        "Daily@Password456",
        IP,
    )

    assert hasher.verified == [("encoded-current", "Current@Password123")]
    assert users.changed[-1]["password_hash"] == "encoded:Daily@Password456"
    with pytest.raises(InvalidCredentials):
        await tokens.verify(login.token)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    (
        (ProviderDisabled("disabled"), 403, "AUTH_PROVIDER_DISABLED"),
        (ProviderUnavailable("down"), 503, "AUTH_PROVIDER_UNAVAILABLE"),
    ),
)
@pytest.mark.asyncio
async def test_facade_maps_provider_errors(
    error: Exception,
    status: int,
    code: str,
) -> None:
    users = FakeUserRepository(account(must_change_password=False))
    tokens = JwtService(SECRET, FakeKeyValue())
    service = AuthFacade(FakeAuthService(error), users, tokens, FakeHasher())

    with pytest.raises(ApiError) as raised:
        await service.login("ad", "user01", "password", IP)

    assert raised.value.status_code == status
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_reauthenticate_current_maps_account_lock_to_explicit_423() -> None:
    service = AuthFacade(
        FakeAuthService(AccountLocked("locked")),
        FakeUserRepository(account(must_change_password=False)),
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="admin",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    with pytest.raises(ApiError) as raised:
        await service.reauthenticate_current(claims, "wrong-password", IP)

    assert raised.value.status_code == 423
    assert raised.value.code == "ACCOUNT_LOCKED"
    assert "wrong-password" not in str(raised.value)


@pytest.mark.asyncio
async def test_reauthenticate_current_reuses_explicit_provider_and_normalized_login() -> None:
    current = account(must_change_password=False)
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="user01",
        external_subject="ad:user01",
        display_name="用户一",
        dept="平台部",
        groups=(),
        account=replace(current, provider_code="ad", login_name="user01"),
    )
    auth = FakeAuthService(identity)
    service = AuthFacade(
        auth,
        FakeUserRepository(current),
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="user01",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    await service.reauthenticate_current(claims, "Current@Password123", IP)

    assert auth.calls == [("ad", "user01", "Current@Password123", IP)]


@pytest.mark.parametrize(
    "identity",
    (
        AuthenticatedIdentity("local", "user01", "local:user01", "", "", ()),
        AuthenticatedIdentity("ad", "another", "ad:another", "", "", ()),
    ),
)
@pytest.mark.asyncio
async def test_reauthenticate_current_rejects_provider_or_identity_switch(
    identity: AuthenticatedIdentity,
) -> None:
    service = AuthFacade(
        FakeAuthService(identity),
        FakeUserRepository(account(must_change_password=False)),
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="user01",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    with pytest.raises(ApiError) as raised:
        await service.reauthenticate_current(claims, "Current@Password123", IP)

    assert raised.value.code == "STEP_UP_REQUIRED"
    assert "Current@Password123" not in str(raised.value)
