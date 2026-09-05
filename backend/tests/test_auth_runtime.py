from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.auth.accounts import AccountSourceConflict, LocalAccountRecord, PlatformAccount
from app.core.auth.backends import (
    AuthenticatedIdentity,
    AuthenticationPurpose,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    ProviderDisabled,
    ProviderUnavailable,
    SessionStateUnavailable,
)
from app.core.auth.jwt import JwtClaims, JwtService
from app.core.auth.principal_context import audit_principal_scope, current_audit_principal
from app.core.auth.runtime import (
    AuthFacade,
    LoginSuccess,
    PasswordChangeRequired,
)
from app.core.auth.service import AccountLocked, RedisKeyValue
from app.core.auth.users import AuthContextChanged, PasswordChangeClaim, PasswordChangeInProgress
from app.core.bounded_executor import ExecutorBackpressure
from app.core.errors import ApiError

IP = "10.0.0.8"
SECRET = "a-jwt-secret-that-is-long-enough-for-hs256-tests"
TAB_ID = "b" * 32


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

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        from app.core.auth.session_policy import eval_memory_session_policy

        policy_result = eval_memory_session_policy(self.values, script, args)
        if policy_result is not None or "auth-session-policy-" in script:
            return policy_result
        if numkeys == 3:
            revoked_jti, revoked_session, refresh_family, _jti_ttl, _session_ttl = args
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


class FakeAuthService:
    def __init__(self, identity: AuthenticatedIdentity | Exception) -> None:
        self.identity = identity
        self.calls: list[tuple[str, str, str, str]] = []
        self.purposes: list[AuthenticationPurpose] = []
        self.bound_successes: list[str] = []

    async def authenticate(
        self,
        provider_code: str,
        login_name: str,
        password: str,
        ip: str,
        *,
        purpose: AuthenticationPurpose = "login",
    ) -> AuthenticatedIdentity:
        self.calls.append((provider_code, login_name, password, ip))
        self.purposes.append(purpose)
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    async def record_bound_success(self, username: str) -> None:
        self.bound_successes.append(username)


class FakeUserRepository:
    def __init__(self, value: PlatformAccount) -> None:
        self.value = value
        self.resolved: list[tuple[AuthenticatedIdentity, str]] = []
        self.changed: list[dict[str, object]] = []
        self.password_change_tokens: dict[str, dict[str, object]] = {}
        self.refresh_audits: list[tuple[PlatformAccount, str]] = []
        self.logout_audits: list[tuple[PlatformAccount, str]] = []
        self.fail_refresh_audit = False
        self.reject_password_cas = False
        self._next_password_token_id = 1

    async def resolve_identity(
        self,
        identity: AuthenticatedIdentity,
        ip: str,
    ) -> PlatformAccount:
        self.resolved.append((identity, ip))
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
            "id": self._next_password_token_id,
            "account_id": account_id,
            "identity_id": identity_id,
            "provider_code": provider_code,
            "login_name": login_name,
            "security_version": security_version,
            "expires_at": expires_at,
            "status": "available",
        }
        self._next_password_token_id += 1

    async def claim_password_change_token(
        self,
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
    ) -> PasswordChangeClaim:
        token = self.password_change_tokens.get(token_hash)
        if token is None or token["status"] not in {"available", "processing"}:
            raise InvalidCredentials("used")
        if token["status"] == "processing":
            raise PasswordChangeInProgress("processing")
        if (
            token["account_id"] != account_id
            or token["identity_id"] != identity_id
            or token["provider_code"] != provider_code
            or token["login_name"] != login_name
            or token["security_version"] != self.value.security_version
        ):
            raise InvalidCredentials("context mismatch")
        lease_id = uuid4()
        lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        token["status"] = "processing"
        token["lease_id"] = lease_id
        token["lease_expires_at"] = lease_expires_at
        return PasswordChangeClaim(
            token_id=int(token["id"]),
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
            current_password_hash="encoded-current",
        )

    async def release_password_change_token(
        self,
        *,
        token_id: int,
        lease_id: UUID,
    ) -> bool:
        token = next(
            (
                candidate
                for candidate in self.password_change_tokens.values()
                if candidate["id"] == token_id
            ),
            None,
        )
        if token is None or token["status"] != "processing" or token.get("lease_id") != lease_id:
            return False
        token["status"] = "available"
        token["lease_id"] = None
        token["lease_expires_at"] = None
        return True

    async def consume_password_change_and_update(
        self,
        *,
        token_id: int,
        lease_id: UUID,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
        password_hash: str,
        actor: str,
        ip: str,
    ) -> None:
        token = next(
            (
                candidate
                for candidate in self.password_change_tokens.values()
                if candidate["id"] == token_id
            ),
            None,
        )
        if token is None or token["status"] != "processing" or token.get("lease_id") != lease_id:
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
        token["lease_id"] = None
        token["lease_expires_at"] = None
        await self.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash=password_hash,
            actor=actor,
            ip=ip,
            expected_security_version=self.value.security_version,
            expected_credential_version=1,
        )

    async def change_local_password(
        self,
        *,
        account_id: int,
        identity_id: int,
        password_hash: str,
        actor: str,
        ip: str,
        expected_security_version: int = 1,
        expected_credential_version: int = 1,
    ) -> None:
        if self.reject_password_cas:
            raise AuthContextChanged("账号安全状态已变化")
        self.changed.append(
            {
                "account_id": account_id,
                "identity_id": identity_id,
                "password_hash": password_hash,
                "actor": actor,
                "ip": ip,
                "expected_security_version": expected_security_version,
                "expected_credential_version": expected_credential_version,
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
        self.logout_audits.append((actor, ip))

    async def audit_refresh(self, actor: PlatformAccount, ip: str) -> None:
        if self.fail_refresh_audit:
            raise RuntimeError("audit unavailable")
        self.refresh_audits.append((actor, ip))


class FakeHasher:
    def __init__(self) -> None:
        self.hashed: list[str] = []
        self.verified: list[tuple[str | None, str]] = []

    def hash(self, password: str) -> str:
        self.hashed.append(password)
        return f"encoded:{password}"

    def verify(self, encoded: str, password: str) -> bool:
        self.verified.append((encoded, password))
        return encoded == "encoded-current" and password == "Current@Password123"

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

    result = await service.login("local", "admin", "Temporary@123", IP, TAB_ID)

    assert isinstance(result, PasswordChangeRequired)
    assert service.auth.purposes == ["login"]
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
async def test_initial_password_change_rejects_current_password_and_releases_claim() -> None:
    service, users, tokens, hasher = facade(must_change_password=True)
    result = await service.login("local", "admin", "Temporary@123", IP, TAB_ID)
    assert isinstance(result, PasswordChangeRequired)
    digest = tokens.password_change_digest(result.change_token)

    with pytest.raises(ApiError) as raised:
        await service.change_initial_password(
            result.change_token,
            "Current@Password123",
            IP,
        )

    assert raised.value.status_code == 422
    assert raised.value.code == "PASSWORD_POLICY_VIOLATION"
    assert users.password_change_tokens[digest]["status"] == "available"
    assert users.changed == []
    assert hasher.hashed == []
    assert hasher.verified == [("encoded-current", "Current@Password123")]


@pytest.mark.asyncio
async def test_initial_password_change_rejects_active_claim_before_hashing() -> None:
    service, users, tokens, hasher = facade(must_change_password=True)
    result = await service.login("local", "admin", "Temporary@123", IP, TAB_ID)
    assert isinstance(result, PasswordChangeRequired)
    digest = tokens.password_change_digest(result.change_token)
    users.password_change_tokens[digest]["status"] = "processing"

    with pytest.raises(ApiError) as raised:
        await service.change_initial_password(
            result.change_token,
            "Another@Password123",
            IP,
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "STATE_CONFLICT"
    assert hasher.verified == []
    assert hasher.hashed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    ((ExecutorBackpressure("full"), "available"), (TimeoutError("slow"), "processing")),
)
async def test_initial_password_change_handles_hash_capacity_without_consuming_token(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_status: str,
) -> None:
    async def fail_hash(*_args: object, **_kwargs: object) -> str:
        assert _kwargs["pool"] == "auth_hash"
        raise failure

    service, users, tokens, _ = facade(must_change_password=True)
    result = await service.login("local", "admin", "Temporary@123", IP, TAB_ID)
    assert isinstance(result, PasswordChangeRequired)
    digest = tokens.password_change_digest(result.change_token)
    monkeypatch.setattr("app.core.auth.runtime.run_bounded", fail_hash)

    with pytest.raises(ApiError) as raised:
        await service.change_initial_password(
            result.change_token,
            "Another@Password123",
            IP,
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "AUTH_PROVIDER_UNAVAILABLE"
    assert users.password_change_tokens[digest]["status"] == expected_status
    assert users.changed == []


@pytest.mark.asyncio
async def test_normal_login_issues_account_based_access_token() -> None:
    service, users, tokens, _ = facade(must_change_password=False)

    result = await service.login("local", "Admin", "Valid@Password123", IP, TAB_ID)

    assert isinstance(result, LoginSuccess)
    claims = await tokens.verify(result.token)
    assert claims.account_id == 8
    assert claims.identity_id == 18
    assert claims.provider_code == "local"
    assert claims.login_name == "admin"
    assert result.expires_in == 900
    assert result.refresh_expires_in == 604800
    refreshed = await service.refresh(result.refresh_token, IP, TAB_ID)
    assert refreshed.refresh_token != result.refresh_token
    assert (await tokens.verify(refreshed.token)).account_id == 8
    assert users.refresh_audits == [(users.value, IP)]


def _viewer_claims() -> JwtClaims:
    return JwtClaims(
        account_id=9,
        identity_id=19,
        provider_code="local",
        login_name="viewer01",
        display_name="查看员",
        dept="平台部",
        role="viewer",
        security_version=3,
    )


@pytest.mark.asyncio
async def test_login_revokes_prior_cookie_family_before_issuing_new_one() -> None:
    service, users, tokens, _ = facade(must_change_password=False)
    prior = await tokens.issue_pair(_viewer_claims(), TAB_ID)
    prior_session_id = str(tokens._decode(prior.token)["sid"])

    with audit_principal_scope():
        result = await service.login(
            "local",
            "admin",
            "Valid@Password123",
            IP,
            TAB_ID,
            prior.refresh_token,
        )
        assert current_audit_principal() is None

    assert isinstance(result, LoginSuccess)
    new_claims = await tokens.verify(result.token)
    assert new_claims.account_id == 8
    assert new_claims.session_id != prior_session_id
    assert users.logout_audits == []
    with pytest.raises(ApiError) as late:
        await service.refresh(prior.refresh_token, IP, TAB_ID)
    assert late.value.status_code == 401
    assert late.value.code == "UNAUTHORIZED"
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(prior.refresh_token, TAB_ID)
    refreshed = await service.refresh(result.refresh_token, IP, TAB_ID)
    assert refreshed.refresh_token != result.refresh_token
    assert (await tokens.verify(refreshed.token)).account_id == 8


@pytest.mark.asyncio
async def test_relogin_revokes_same_account_prior_family() -> None:
    service, users, tokens, _ = facade(must_change_password=False)
    first = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(first, LoginSuccess)
    first_session_id = (await tokens.verify(first.token)).session_id

    second = await service.login(
        "local",
        "admin",
        "Valid@Password123",
        IP,
        TAB_ID,
        first.refresh_token,
    )
    assert isinstance(second, LoginSuccess)
    assert second.refresh_token != first.refresh_token
    assert (await tokens.verify(second.token)).session_id != first_session_id
    with pytest.raises(ApiError) as late:
        await service.refresh(first.refresh_token, IP, TAB_ID)
    assert late.value.code == "UNAUTHORIZED"
    third = await service.refresh(second.refresh_token, IP, TAB_ID)
    assert third.refresh_token != second.refresh_token
    assert users.logout_audits == []


@pytest.mark.asyncio
async def test_failed_login_does_not_revoke_presented_cookie_family() -> None:
    service, _, tokens, _ = facade(must_change_password=False)
    first = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(first, LoginSuccess)
    service.auth.identity = InvalidCredentials("bad")

    with pytest.raises(ApiError) as failed:
        await service.login(
            "local",
            "admin",
            "Wrong@Password123",
            IP,
            TAB_ID,
            first.refresh_token,
        )
    assert failed.value.code == "UNAUTHORIZED"
    reused = await service.refresh(first.refresh_token, IP, TAB_ID)
    assert (await tokens.verify(reused.token)).account_id == 8


@pytest.mark.asyncio
async def test_password_change_required_does_not_revoke_prior_family() -> None:
    service, _, tokens, _ = facade(must_change_password=True)
    prior = await tokens.issue_pair(
        JwtClaims(8, 18, "local", "admin", "管理员", "平台部", "admin", 3),
        TAB_ID,
    )

    result = await service.login(
        "local",
        "admin",
        "Temporary@123",
        IP,
        TAB_ID,
        prior.refresh_token,
    )

    assert isinstance(result, PasswordChangeRequired)
    rotated = await tokens.rotate_refresh(prior.refresh_token, TAB_ID)
    assert (await tokens.verify(rotated.token)).account_id == 8


@pytest.mark.asyncio
async def test_login_ignores_invalid_prior_cookie_and_still_issues_family() -> None:
    service, _, tokens, _ = facade(must_change_password=False)

    result = await service.login(
        "local",
        "admin",
        "Valid@Password123",
        IP,
        TAB_ID,
        "not-a-refresh-token",
    )

    assert isinstance(result, LoginSuccess)
    refreshed = await service.refresh(result.refresh_token, IP, TAB_ID)
    assert (await tokens.verify(refreshed.token)).account_id == 8


@pytest.mark.asyncio
async def test_prior_family_revoke_unavailability_fail_closes_without_new_pair() -> None:
    value = account(must_change_password=False)
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

    class RevokeUnavailable(FakeKeyValue):
        async def eval(self, script: str, numkeys: int, *args: object) -> object:
            if numkeys == 3:
                raise RuntimeError("redis unavailable")
            return await super().eval(script, numkeys, *args)

    tokens = JwtService(
        SECRET,
        RevokeUnavailable(),
        clock=lambda: datetime(2026, 7, 16, 8, tzinfo=UTC),
        security_session_loader=users.load_security_session,
    )
    service = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    prior = await tokens.issue_pair(
        JwtClaims(8, 18, "local", "admin", "管理员", "平台部", "admin", 3),
        TAB_ID,
    )

    with pytest.raises(ApiError) as raised:
        await service.login(
            "local",
            "admin",
            "Valid@Password123",
            IP,
            TAB_ID,
            prior.refresh_token,
        )
    assert raised.value.status_code == 503
    assert raised.value.code == "AUTH_SESSION_UNAVAILABLE"
    reused = await tokens.rotate_refresh(prior.refresh_token, TAB_ID)
    assert (await tokens.verify(reused.token)).account_id == 8


@pytest.mark.asyncio
async def test_login_prior_family_revoke_does_not_log_tokens_or_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, tokens, _ = facade(must_change_password=False)
    prior = await tokens.issue_pair(_viewer_claims(), TAB_ID)

    with caplog.at_level(logging.DEBUG):
        result = await service.login(
            "local",
            "admin",
            "Valid@Password123",
            IP,
            TAB_ID,
            prior.refresh_token,
        )

    assert isinstance(result, LoginSuccess)
    leaked = caplog.text
    assert prior.refresh_token not in leaked
    assert result.refresh_token not in leaked
    assert result.token not in leaked
    assert "Valid@Password123" not in leaked
    assert "13800138000" not in leaked


@pytest.mark.asyncio
async def test_account_source_conflict_does_not_record_bound_login_success() -> None:
    value = account(must_change_password=False)
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="admin",
        external_subject="ad:admin",
        display_name="冲突账号",
        dept="平台部",
        groups=(),
    )
    auth = FakeAuthService(identity)
    users = FakeUserRepository(value)

    async def conflict(_: AuthenticatedIdentity, __: str) -> PlatformAccount:
        raise AccountSourceConflict("owned by local")

    users.resolve_identity = conflict  # type: ignore[method-assign]
    service = AuthFacade(auth, users, JwtService(SECRET, FakeKeyValue()), FakeHasher())

    with pytest.raises(ApiError) as raised:
        await service.login("ad", "admin", "Valid@Password123", IP, TAB_ID)

    assert raised.value.code == "ACCOUNT_SOURCE_CONFLICT"
    assert auth.bound_successes == []


@pytest.mark.asyncio
async def test_refresh_audit_failure_revokes_successor_session() -> None:
    service, users, tokens, _ = facade(must_change_password=False)
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)
    users.fail_refresh_audit = True

    with pytest.raises(ApiError) as raised:
        await service.refresh(login.refresh_token, IP, TAB_ID)

    assert raised.value.code == "AUTH_SESSION_UNAVAILABLE"
    with pytest.raises(InvalidCredentials):
        await tokens.verify(login.token)


@pytest.mark.asyncio
async def test_facade_verify_binds_stable_audit_principal() -> None:
    service, _, _, _ = facade(must_change_password=False)
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)

    with audit_principal_scope():
        claims = await service.verify(login.token)
        principal = current_audit_principal()

    assert principal == claims.principal
    assert principal is not None
    assert principal.account_id == 8
    assert principal.identity_id == 18


@pytest.mark.asyncio
async def test_daily_password_change_checks_current_password_and_revokes_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_pools: list[str] = []

    async def inline_hash(
        function: Callable[..., object],
        *args: object,
        pool: str,
        **_kwargs: object,
    ) -> object:
        observed_pools.append(pool)
        return function(*args)

    monkeypatch.setattr("app.core.auth.runtime.run_bounded", inline_hash)
    service, users, tokens, hasher = facade(must_change_password=False)
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)

    await service.change_password(
        login.token,
        "Current@Password123",
        "Daily@Password456",
        IP,
    )

    assert hasher.verified == []
    assert service.auth.calls[-1] == ("local", "admin", "Current@Password123", IP)
    assert service.auth.purposes[-1] == "reauthentication"
    assert observed_pools == ["auth_hash"]
    assert users.changed[-1]["password_hash"] == "encoded:Daily@Password456"
    with pytest.raises(InvalidCredentials):
        await tokens.verify(login.token)


@pytest.mark.asyncio
async def test_stale_password_change_does_not_write_success_audit() -> None:
    service, users, tokens, _ = facade(must_change_password=False)
    users.reject_password_cas = True
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)

    with pytest.raises(ApiError) as raised:
        await service.change_password(
            login.token,
            "Current@Password123",
            "Daily@Password456",
            IP,
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "AUTH_CONTEXT_CHANGED"
    assert raised.value.message == "账号安全状态已变化，请重新登录后重试"
    assert users.changed == []


@pytest.mark.asyncio
async def test_daily_password_change_rejects_same_password_after_reauthentication() -> None:
    service, users, tokens, hasher = facade(must_change_password=False)
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)

    with pytest.raises(ApiError) as raised:
        await service.change_password(
            login.token,
            "Current@Password123",
            "Current@Password123",
            IP,
        )

    assert raised.value.status_code == 422
    assert raised.value.code == "PASSWORD_POLICY_VIOLATION"
    assert service.auth.calls[-1] == ("local", "admin", "Current@Password123", IP)
    assert users.changed == []
    assert hasher.hashed == []


@pytest.mark.asyncio
async def test_daily_password_change_rejects_ad_before_password_provider_work() -> None:
    value = replace(account(must_change_password=False), provider_code="ad")
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="admin",
        external_subject="ad:admin",
        display_name="管理员",
        dept="平台部",
        groups=(),
        account=value,
    )
    auth = FakeAuthService(identity)
    users = FakeUserRepository(value)
    tokens = JwtService(
        SECRET,
        FakeKeyValue(),
        security_session_loader=users.load_security_session,
    )
    service = AuthFacade(auth, users, tokens, FakeHasher())
    pair = await tokens.issue_pair(
        JwtClaims(8, 18, "ad", "admin", "管理员", "平台部", "admin", 3),
        TAB_ID,
    )

    with pytest.raises(ApiError) as raised:
        await service.change_password(
            pair.token,
            "Current@Password123",
            "Another@Password123",
            IP,
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "STATE_CONFLICT"
    assert auth.calls == []
    assert users.changed == []


@pytest.mark.asyncio
async def test_daily_password_change_maps_wrong_password_to_unauthorized() -> None:
    service, _, _, _ = facade(must_change_password=False)
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)
    service.auth.identity = InvalidCredentials("bad")

    with pytest.raises(ApiError) as error:
        await service.change_password(login.token, "Wrong@Password123", "Daily@Password456", IP)

    assert error.value.status_code == 401
    assert error.value.code == "UNAUTHORIZED"
    assert error.value.message == "当前密码错误"
    assert service.auth.calls[-1] == ("local", "admin", "Wrong@Password123", IP)


@pytest.mark.asyncio
async def test_login_maps_session_state_unavailable_to_503() -> None:
    value = account(must_change_password=False)
    users = FakeUserRepository(value)
    tokens = JwtService(
        SECRET,
        FakeKeyValue(),
        clock=lambda: datetime(2026, 7, 16, 8, tzinfo=UTC),
        security_session_loader=users.load_security_session,
    )
    service = AuthFacade(
        FakeAuthService(SessionStateUnavailable("redis down")),
        users,
        tokens,
        FakeHasher(),
    )

    with pytest.raises(ApiError) as error:
        await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)

    assert error.value.status_code == 503
    assert error.value.code == "AUTH_SESSION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_redis_key_value_maps_redis_errors_to_session_unavailable() -> None:
    class BrokenRedis:
        async def get(self, key: str) -> object:
            raise RedisConnectionError("down")

        async def set(self, key: str, value: object, *, ex: int) -> None:
            raise RedisConnectionError("down")

        async def delete(self, key: str) -> None:
            raise RedisConnectionError("down")

        async def eval(self, *args: object) -> object:
            raise RedisConnectionError("down")

    store = RedisKeyValue(BrokenRedis())  # type: ignore[arg-type]
    with pytest.raises(SessionStateUnavailable):
        await store.get("k")
    with pytest.raises(SessionStateUnavailable):
        await store.set("k", "v", ex=1)
    with pytest.raises(SessionStateUnavailable):
        await store.delete("k")
    with pytest.raises(SessionStateUnavailable):
        await store.increment("k", window_s=60)
    with pytest.raises(SessionStateUnavailable):
        await store.eval("return 1", 0)


@pytest.mark.asyncio
async def test_logout_uses_refresh_family_when_access_token_has_expired() -> None:
    value = account(must_change_password=False)
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
    moments = [datetime(2026, 7, 16, 8, tzinfo=UTC)]
    tokens = JwtService(
        SECRET,
        FakeKeyValue(),
        clock=lambda: moments[0],
        ttl=timedelta(minutes=15),
        security_session_loader=users.load_security_session,
    )
    service = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    login = await service.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)
    moments[0] += timedelta(minutes=16)

    await service.logout(login.token, IP, login.refresh_token)

    assert users.logout_audits == [(value, IP)]
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(login.refresh_token, TAB_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_account_id", (8, 9))
async def test_logout_revokes_bearer_and_mismatched_cookie_families(
    cookie_account_id: int,
) -> None:
    """全局 refresh cookie 与标签页 bearer 错配时，两个 family 都必须失效。"""

    value = account(must_change_password=False)
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
    tokens = JwtService(SECRET, FakeKeyValue())
    service = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    bearer_family = await tokens.issue_pair(
        JwtClaims(8, 18, "local", "admin", "管理员", "平台部", "admin", 3),
        TAB_ID,
    )
    cookie_family = await tokens.issue_pair(
        JwtClaims(
            cookie_account_id,
            18 if cookie_account_id == 8 else 19,
            "local",
            "admin" if cookie_account_id == 8 else "viewer01",
            "管理员" if cookie_account_id == 8 else "查看员",
            "平台部",
            "admin" if cookie_account_id == 8 else "viewer",
            3,
        ),
        TAB_ID,
    )

    await service.logout(bearer_family.token, IP, cookie_family.refresh_token)

    with pytest.raises(InvalidCredentials):
        await tokens.verify(bearer_family.token)
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(cookie_family.refresh_token, TAB_ID)
    assert users.logout_audits == [(value, IP)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_account_id", (8, 9))
async def test_logout_revokes_expired_bearer_and_mismatched_cookie_families(
    cookie_account_id: int,
) -> None:
    """过期 bearer 只用于吊销自身 family，审计主体必须来自有效 cookie。"""

    value = account(must_change_password=False)
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
    moments = [datetime(2026, 7, 16, 8, tzinfo=UTC)]
    tokens = JwtService(
        SECRET,
        FakeKeyValue(),
        clock=lambda: moments[0],
        ttl=timedelta(minutes=15),
        security_session_loader=users.load_security_session,
    )
    service = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    bearer_family = await tokens.issue_pair(
        JwtClaims(8, 18, "local", "admin", "管理员", "平台部", "admin", 3),
        TAB_ID,
    )
    cookie_claims = JwtClaims(
        cookie_account_id,
        18 if cookie_account_id == 8 else 19,
        "local",
        "admin" if cookie_account_id == 8 else "viewer01",
        "管理员" if cookie_account_id == 8 else "查看员",
        "平台部",
        "admin" if cookie_account_id == 8 else "viewer",
        3,
    )
    cookie_family = await tokens.issue_pair(cookie_claims, TAB_ID)
    moments[0] += timedelta(minutes=16)

    await service.logout(bearer_family.token, IP, cookie_family.refresh_token)

    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(bearer_family.refresh_token, TAB_ID)
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(cookie_family.refresh_token, TAB_ID)
    assert len(users.logout_audits) == 1
    assert users.logout_audits[0][0].account_id == cookie_account_id


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
        await service.login("ad", "user01", "password", IP, TAB_ID)

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
async def test_reauthenticate_current_maps_provider_capacity_to_safe_503() -> None:
    service = AuthFacade(
        FakeAuthService(ProviderCapacityUnavailable("busy")),
        FakeUserRepository(account(must_change_password=False)),
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="admin",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    with pytest.raises(ApiError) as raised:
        await service.reauthenticate_current(claims, "password", IP)

    assert raised.value.status_code == 503
    assert raised.value.code == "AUTH_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_reauthenticate_current_maps_provider_outage_to_safe_503() -> None:
    service = AuthFacade(
        FakeAuthService(ProviderUnavailable("directory unavailable")),
        FakeUserRepository(account(must_change_password=False)),
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="admin",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    with pytest.raises(ApiError) as raised:
        await service.reauthenticate_current(claims, "password", IP)

    assert raised.value.status_code == 503
    assert raised.value.code == "AUTH_PROVIDER_UNAVAILABLE"


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
        dept="平台部",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    await service.reauthenticate_current(claims, "Current@Password123", IP)

    assert auth.calls == [("ad", "user01", "Current@Password123", IP)]


@pytest.mark.asyncio
async def test_reauthenticate_current_resolves_external_subject_and_current_role() -> None:
    current = replace(
        account(must_change_password=False),
        provider_code="ad",
        login_name="user01",
        normalized_login_name="user01",
    )
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="user01",
        external_subject="directory-guid-001",
        display_name="用户一",
        dept="平台部",
        groups=("CN=SMS-Admins",),
    )
    users = FakeUserRepository(current)
    service = AuthFacade(
        FakeAuthService(identity),
        users,
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="user01",
        dept="平台部",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    await service.reauthenticate_current(claims, "Current@Password123", IP)

    assert users.resolved == [(identity, IP)]


@pytest.mark.asyncio
async def test_reauthenticate_current_rejects_external_authority_change() -> None:
    synchronized = replace(
        account(must_change_password=False),
        provider_code="ad",
        login_name="user01",
        normalized_login_name="user01",
        role="operator",
        security_version=4,
    )
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="user01",
        external_subject="directory-guid-001",
        display_name="用户一",
        dept="平台部",
        groups=("CN=SMS-Operators",),
    )
    service = AuthFacade(
        FakeAuthService(identity),
        FakeUserRepository(synchronized),
        JwtService(SECRET, FakeKeyValue()),
        FakeHasher(),
    )
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="user01",
        dept="平台部",
        role="admin",
        security_version=3,
        jti="session-1",
    )

    with pytest.raises(ApiError) as raised:
        await service.reauthenticate_current(claims, "Current@Password123", IP)

    assert raised.value.code == "STEP_UP_REQUIRED"


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
