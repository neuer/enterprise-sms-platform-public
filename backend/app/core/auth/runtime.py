"""认证组件生产装配，以及登录、改密和会话应用用例。"""

from __future__ import annotations

import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal, Protocol

from app.core.auth.accounts import AccountNotFound, AccountSourceConflict, PlatformAccount
from app.core.auth.backends import (
    AuthenticatedIdentity,
    AuthenticationPurpose,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    ProviderDisabled,
    ProviderUnavailable,
    SessionStateUnavailable,
)
from app.core.auth.guard_policy import SqlAuthGuardPolicyLoader
from app.core.auth.identity import normalize_login_name
from app.core.auth.jwt import (
    IssuedTokenPair,
    JwtClaims,
    JwtService,
    ReauthenticationRequired,
)
from app.core.auth.passwords import (
    LocalPasswordHasher,
    PasswordPolicy,
    PasswordPolicyViolation,
)
from app.core.auth.principal_context import bind_audit_principal
from app.core.auth.providers import create_provider_registry
from app.core.auth.security_events import SqlAuthSecurityEventRepository
from app.core.auth.service import (
    AccountLocked,
    AuthService,
    LoginGuard,
    RateLimited,
    RedisKeyValue,
)
from app.core.auth.session_policy_sync import AlignedAuthSessionPolicyLoader
from app.core.auth.users import (
    AuthContextChanged,
    PasswordChangeAuthorization,
    PasswordChangeClaim,
    PasswordChangeInProgress,
    SqlUserRepository,
    UserRepository,
)
from app.core.bounded_executor import ExecutorBackpressure, run_bounded
from app.core.errors import ApiError
from app.services.auth_provider import AuthProviderService, ProviderSummary
from app.services.auth_provider_repository import SqlAuthProviderRepository
from app.services.user_management import LastAdminProtected
from app.settings import Settings, get_settings

PASSWORD_CHANGE_EXPIRES_IN = 600


@dataclass(frozen=True, slots=True)
class LoginSuccess:
    token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    user: PlatformAccount


@dataclass(frozen=True, slots=True)
class PasswordChangeRequired:
    change_token: str
    expires_in: int = PASSWORD_CHANGE_EXPIRES_IN
    next_action: Literal["change_password"] = "change_password"
    token: None = None


class AuthenticationService(Protocol):
    async def authenticate(
        self,
        provider_code: str,
        login_name: str,
        password: str,
        ip: str,
        *,
        purpose: AuthenticationPurpose = "login",
    ) -> AuthenticatedIdentity: ...

    async def record_bound_success(self, username: str) -> None: ...


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, encoded: str, password: str) -> bool: ...

    def verify_or_dummy(self, encoded: str | None, password: str) -> bool: ...


class AuthFacade:
    """登录、首次/日常改密、登出与管理员强制下线的应用门面。"""

    def __init__(
        self,
        auth: AuthenticationService,
        users: UserRepository,
        tokens: JwtService,
        passwords: PasswordHasher | None = None,
        policy: PasswordPolicy | None = None,
        providers: AuthProviderService | None = None,
    ) -> None:
        self.auth = auth
        self.users = users
        self.tokens = tokens
        self.passwords = passwords or LocalPasswordHasher()
        self.policy = policy or PasswordPolicy()
        self.providers = providers

    async def list_providers(self) -> tuple[ProviderSummary, ...]:
        if self.providers is None:
            raise ApiError(
                503,
                "AUTH_PROVIDER_UNAVAILABLE",
                "认证源列表暂不可用",
                None,
            )
        return await self.providers.list_enabled()

    def password_policy(self) -> dict[str, int | bool | str]:
        return self.policy.public_contract()

    async def _record_bound_success(self, account: PlatformAccount) -> None:
        """账号归属确认后才允许清除规范化登录名的失败/锁定状态。"""

        await self.auth.record_bound_success(account.normalized_login_name)

    async def reauthenticate_current(
        self,
        claims: JwtClaims,
        password: str,
        ip: str,
    ) -> PasswordChangeAuthorization | None:
        """只用当前显式 Provider 重验密码，并拒绝认证身份切换。"""

        try:
            identity = await self.auth.authenticate(
                claims.provider_code,
                claims.login_name,
                password,
                ip,
                purpose="reauthentication",
            )
        except AccountLocked:
            raise ApiError(
                423,
                "ACCOUNT_LOCKED",
                "账号失败次数已达阈值，请使用正确凭据重试",
                None,
            ) from None
        except RateLimited as error:
            raise ApiError(429, "RATE_LIMITED", str(error), None) from None
        except ProviderCapacityUnavailable:
            raise ApiError(
                503,
                "AUTH_PROVIDER_UNAVAILABLE",
                "认证源容量暂不可用，请稍后重试",
                None,
            ) from None
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None
        except ProviderUnavailable:
            raise ApiError(
                503,
                "AUTH_PROVIDER_UNAVAILABLE",
                "认证源暂不可用，请稍后重试",
                None,
            ) from None
        except (
            InvalidCredentials,
            ProviderDisabled,
        ):
            raise ApiError(401, "STEP_UP_REQUIRED", "二次认证失败", None) from None
        same_identity = identity.provider_code == claims.provider_code and normalize_login_name(
            identity.login_name
        ) == normalize_login_name(claims.login_name)
        try:
            account = (
                identity.account
                if identity.account is not None
                else await self.users.resolve_identity(identity, ip)
            )
        except (AccountSourceConflict, InvalidCredentials, LastAdminProtected):
            raise ApiError(401, "STEP_UP_REQUIRED", "二次认证失败", None) from None
        same_identity = same_identity and (
            account.account_id == claims.account_id
            and account.identity_id == claims.identity_id
            and account.provider_code == claims.provider_code
            and normalize_login_name(account.login_name) == normalize_login_name(claims.login_name)
            and account.dept == claims.dept
            and account.role == claims.role
            and account.security_version == claims.security_version
        )
        if not same_identity:
            raise ApiError(401, "STEP_UP_REQUIRED", "二次认证失败", None)
        await self._record_bound_success(account)
        if claims.provider_code != "local":
            return None
        record = await self.users.find_local_account(account.normalized_login_name)
        if record is None or record.account.account_id != account.account_id:
            raise ApiError(401, "STEP_UP_REQUIRED", "二次认证失败", None)
        return PasswordChangeAuthorization(
            account_id=account.account_id,
            identity_id=account.identity_id,
            provider_code="local",
            normalized_login_name=account.normalized_login_name,
            expected_security_version=account.security_version,
            expected_credential_version=record.credential_version,
        )

    async def login(
        self,
        provider_code: str,
        login_name: str,
        password: str,
        ip: str,
        tab_id: str,
        prior_refresh_token: str | None = None,
    ) -> LoginSuccess | PasswordChangeRequired:
        try:
            identity = await self.auth.authenticate(
                provider_code,
                login_name,
                password,
                ip,
                purpose="login",
            )
            user = await self.users.resolve_identity(identity, ip)
            await self._record_bound_success(user)
        except AccountLocked:
            raise ApiError(
                423,
                "ACCOUNT_LOCKED",
                "账号失败次数已达阈值，请使用正确凭据重试",
                None,
            ) from None
        except RateLimited as error:
            raise ApiError(429, "RATE_LIMITED", str(error), None) from None
        except LastAdminProtected as error:
            raise ApiError(409, "LAST_ADMIN_PROTECTED", str(error), None) from None
        except InvalidCredentials:
            raise ApiError(401, "UNAUTHORIZED", "用户名或密码错误", None) from None
        except ProviderDisabled:
            raise ApiError(
                403,
                "AUTH_PROVIDER_DISABLED",
                "所选认证源未启用",
                None,
            ) from None
        except ProviderUnavailable:
            raise ApiError(
                503,
                "AUTH_PROVIDER_UNAVAILABLE",
                "所选认证源暂不可用",
                None,
            ) from None
        except AccountSourceConflict:
            raise ApiError(
                409,
                "ACCOUNT_SOURCE_CONFLICT",
                "该登录名已由其他认证源占用，请联系管理员",
                None,
            ) from None
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None
        if user.must_change_password:
            change_token = self.tokens.issue_password_change(
                account_id=user.account_id,
                identity_id=user.identity_id,
                provider_code=user.provider_code,
                login_name=user.login_name,
            )
            change_claims = self.tokens.read_password_change(change_token)
            await self.users.create_password_change_token(
                token_hash=self.tokens.password_change_digest(change_token),
                account_id=user.account_id,
                identity_id=user.identity_id,
                provider_code=user.provider_code,
                login_name=user.normalized_login_name,
                security_version=user.security_version,
                expires_at=datetime.fromtimestamp(change_claims.expires_at, tz=UTC),
            )
            return PasswordChangeRequired(change_token)
        await self._revoke_presented_refresh_family(prior_refresh_token)
        try:
            pair = await self.tokens.issue_pair(self._claims(user), tab_id)
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None
        return self._login_success(pair, user)

    async def _revoke_presented_refresh_family(
        self,
        refresh_token: str | None,
    ) -> None:
        """吊销请求带来的旧 Cookie family；无效令牌忽略，存储故障失败关闭。"""

        if not refresh_token:
            return
        try:
            await self.tokens.revoke_refresh_token(refresh_token)
        except InvalidCredentials:
            return
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None

    async def refresh(self, refresh_token: str, ip: str, tab_id: str) -> LoginSuccess:
        """轮换后持久审计；审计失败时吊销刚签发的整个会话。"""

        try:
            pair = await self.tokens.rotate_refresh(refresh_token, tab_id)
            claims = await self.tokens.verify(pair.token)
        except ReauthenticationRequired:
            raise ApiError(
                401,
                "AUTH_REAUTH_REQUIRED",
                "AD 会话已到期，请重新登录",
                None,
            ) from None
        except InvalidCredentials:
            raise ApiError(401, "UNAUTHORIZED", "刷新令牌无效或已使用", None) from None
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None
        account = self._account(claims)
        bind_audit_principal(claims.principal)
        try:
            await self.users.audit_refresh(account, ip)
        except Exception:
            # Redis 不可用时 verify 本身失败关闭；不得把未审计令牌返回调用方。
            with suppress(Exception):
                await self.tokens.revoke_token(pair.token)
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话审计暂不可用，请重新登录",
                None,
            ) from None
        return self._login_success(pair, account)

    async def change_initial_password(
        self,
        change_token: str,
        new_password: str,
        ip: str = "0.0.0.0",
    ) -> None:
        try:
            claims = self.tokens.read_password_change(change_token)
            self.policy.validate(new_password, username=claims.login_name)
            claim = await self.users.claim_password_change_token(
                token_hash=self.tokens.password_change_digest(change_token),
                account_id=claims.account_id,
                identity_id=claims.identity_id,
                provider_code=claims.provider_code,
                login_name=normalize_login_name(claims.login_name),
            )
            try:
                same_password = await run_bounded(
                    self.passwords.verify,
                    claim.current_password_hash,
                    new_password,
                    timeout_s=5,
                    pool="auth_hash",
                )
            except ExecutorBackpressure:
                await self._release_password_change_claim(claim)
                raise ApiError(
                    503,
                    "AUTH_PROVIDER_UNAVAILABLE",
                    "认证源容量暂不可用，请稍后重试",
                    None,
                ) from None
            except TimeoutError:
                # shield 下底层 Argon2 仍可能运行；保留短租约，禁止立即重放。
                raise ApiError(
                    503,
                    "AUTH_PROVIDER_UNAVAILABLE",
                    "认证源容量暂不可用，请稍后重试",
                    None,
                ) from None
            if same_password:
                await self._release_password_change_claim(claim)
                raise PasswordPolicyViolation("新密码不能与当前密码相同")
            try:
                password_hash = await run_bounded(
                    self.passwords.hash,
                    new_password,
                    timeout_s=5,
                    pool="auth_hash",
                )
            except ExecutorBackpressure:
                await self._release_password_change_claim(claim)
                raise ApiError(
                    503,
                    "AUTH_PROVIDER_UNAVAILABLE",
                    "认证源容量暂不可用，请稍后重试",
                    None,
                ) from None
            except TimeoutError:
                raise ApiError(
                    503,
                    "AUTH_PROVIDER_UNAVAILABLE",
                    "认证源容量暂不可用，请稍后重试",
                    None,
                ) from None
            await self.users.consume_password_change_and_update(
                token_id=claim.token_id,
                lease_id=claim.lease_id,
                account_id=claims.account_id,
                identity_id=claims.identity_id,
                provider_code=claims.provider_code,
                login_name=normalize_login_name(claims.login_name),
                password_hash=password_hash,
                actor=claims.login_name,
                ip=ip,
            )
        except PasswordPolicyViolation as error:
            raise ApiError(
                422,
                "PASSWORD_POLICY_VIOLATION",
                str(error),
                None,
            ) from None
        except PasswordChangeInProgress:
            raise ApiError(
                409,
                "STATE_CONFLICT",
                "改密请求处理中，请稍后重试",
                None,
            ) from None
        except InvalidCredentials:
            raise ApiError(
                401,
                "UNAUTHORIZED",
                "改密令牌无效、已过期或已使用",
                None,
            ) from None
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None

    async def _release_password_change_claim(
        self,
        claim: PasswordChangeClaim,
    ) -> None:
        released = await self.users.release_password_change_token(
            token_id=claim.token_id,
            lease_id=claim.lease_id,
        )
        if not released:
            raise PasswordChangeInProgress("改密租约已变化")

    async def change_password(
        self,
        token: str,
        current_password: str,
        new_password: str,
        ip: str,
    ) -> None:
        claims = await self.verify(token)
        if claims.provider_code != "local":
            raise ApiError(
                409,
                "STATE_CONFLICT",
                "仅本地账号支持修改密码",
                None,
            )
        try:
            authorization = await self.reauthenticate_current(claims, current_password, ip)
        except ApiError as error:
            if error.code == "STEP_UP_REQUIRED":
                raise ApiError(401, "UNAUTHORIZED", "当前密码错误", None) from None
            raise
        if authorization is None:
            raise ApiError(409, "STATE_CONFLICT", "仅本地账号支持修改密码", None)
        if secrets.compare_digest(
            current_password.encode("utf-8"),
            new_password.encode("utf-8"),
        ):
            raise ApiError(
                422,
                "PASSWORD_POLICY_VIOLATION",
                "新密码不能与当前密码相同",
                None,
            )
        try:
            self.policy.validate(new_password, username=claims.login_name)
        except PasswordPolicyViolation as error:
            raise ApiError(
                422,
                "PASSWORD_POLICY_VIOLATION",
                str(error),
                None,
            ) from None
        try:
            password_hash = await run_bounded(
                self.passwords.hash,
                new_password,
                timeout_s=5,
                pool="auth_hash",
            )
        except (ExecutorBackpressure, TimeoutError):
            raise ApiError(
                503,
                "AUTH_PROVIDER_UNAVAILABLE",
                "认证源容量暂不可用，请稍后重试",
                None,
            ) from None
        try:
            await self.users.change_local_password(
                account_id=authorization.account_id,
                identity_id=authorization.identity_id,
                password_hash=password_hash,
                actor=claims.login_name,
                ip=ip,
                expected_security_version=authorization.expected_security_version,
                expected_credential_version=authorization.expected_credential_version,
            )
        except AuthContextChanged:
            raise ApiError(
                409,
                "AUTH_CONTEXT_CHANGED",
                "账号安全状态已变化，请重新登录后重试",
                None,
            ) from None

    async def verify(self, token: str) -> JwtClaims:
        try:
            claims = await self.tokens.verify(token)
            bind_audit_principal(claims.principal)
            return claims
        except ReauthenticationRequired:
            raise ApiError(
                401,
                "AUTH_REAUTH_REQUIRED",
                "AD 会话已到期，请重新登录",
                None,
            ) from None
        except InvalidCredentials:
            raise ApiError(401, "UNAUTHORIZED", "无效或已吊销的令牌", None) from None
        except SessionStateUnavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None

    async def logout(
        self,
        token: str,
        ip: str,
        refresh_token: str | None = None,
    ) -> None:
        """分别吊销请求携带的 access 与 refresh family，禁止跨标签页错配残留。"""

        access_claims: JwtClaims | None = None
        refresh_claims: JwtClaims | None = None
        access_error: ApiError | None = None
        revocation_unavailable = False
        try:
            access_claims = await self.verify(token)
        except ApiError as error:
            access_error = error

        # 吊销不等于认证：经签名但已过期的 access 仍必须删除自身 family，
        # 但不可用作当前主体或审计主体。
        try:
            await self.tokens.revoke_token(token)
        except InvalidCredentials:
            pass
        except SessionStateUnavailable:
            revocation_unavailable = True

        if refresh_token is not None:
            try:
                refresh_claims = await self.tokens.revoke_refresh_token(refresh_token)
            except InvalidCredentials:
                # 已失效或损坏的 cookie 不代表仍有可撤销 family；有效 bearer 仍正常登出。
                refresh_claims = None
            except SessionStateUnavailable:
                revocation_unavailable = True

        if revocation_unavailable:
            raise ApiError(
                503,
                "AUTH_SESSION_UNAVAILABLE",
                "会话权威状态暂不可用，请稍后重试",
                None,
            ) from None

        claims = access_claims or refresh_claims
        if claims is None:
            if access_error is not None:
                raise access_error
            raise ApiError(401, "UNAUTHORIZED", "无效或已吊销的令牌", None)
        if access_claims is None:
            bind_audit_principal(claims.principal)
        await self.users.audit_logout(self._account(claims), ip)

    async def force_logout(self, token: str, account_id: int, ip: str) -> None:
        actor = await self.verify(token)
        if actor.role != "admin":
            raise ApiError(403, "FORBIDDEN", "仅管理员可执行强制下线", None)
        try:
            await self.users.invalidate_sessions(
                self._account(actor),
                account_id,
                ip,
            )
        except AccountNotFound:
            raise ApiError(404, "NOT_FOUND", "账号不存在", None) from None

    @staticmethod
    def _claims(user: PlatformAccount) -> JwtClaims:
        return JwtClaims(
            account_id=user.account_id,
            identity_id=user.identity_id,
            provider_code=user.provider_code,
            login_name=user.login_name,
            display_name=user.display_name,
            dept=user.dept,
            role=user.role,
            security_version=user.security_version,
        )

    @staticmethod
    def _account(claims: JwtClaims) -> PlatformAccount:
        return PlatformAccount(
            account_id=claims.account_id,
            identity_id=claims.identity_id,
            provider_code=claims.provider_code,
            login_name=claims.login_name,
            normalized_login_name=claims.login_name.casefold(),
            display_name=claims.display_name,
            dept=claims.dept,
            role=claims.role,
            security_version=claims.security_version,
            account_enabled=True,
            identity_enabled=True,
            provider_enabled=True,
        )

    @staticmethod
    def _login_success(pair: IssuedTokenPair, user: PlatformAccount) -> LoginSuccess:
        return LoginSuccess(
            pair.token,
            pair.refresh_token,
            pair.expires_in,
            pair.refresh_expires_in,
            user,
        )


def create_auth_facade(settings: Settings) -> AuthFacade:
    store = RedisKeyValue.from_url(settings.redis_auth_url)
    users = SqlUserRepository(settings)
    provider_repository = SqlAuthProviderRepository(settings)
    passwords = LocalPasswordHasher()
    providers = create_provider_registry(
        settings=settings,
        provider_repository=provider_repository,
        local_repository=users,
        local_passwords=passwords,
    )
    guard_policy = SqlAuthGuardPolicyLoader(settings)
    auth = AuthService(
        providers,
        LoginGuard(
            store,
            policy_loader=guard_policy.load,
            security_events=SqlAuthSecurityEventRepository(settings),
        ),
    )
    session_policy = AlignedAuthSessionPolicyLoader(store, settings=settings)
    tokens = JwtService(
        settings.credential("jwt_secret"),
        store,
        accept_legacy=settings.jwt_accept_legacy,
        security_session_loader=users.load_security_session,
        session_policy_loader=session_policy.load,
    )
    return AuthFacade(
        auth,
        users,
        tokens,
        passwords=passwords,
        providers=AuthProviderService(provider_repository, providers),
    )


@lru_cache
def get_auth_facade() -> AuthFacade:
    return create_auth_facade(get_settings())
