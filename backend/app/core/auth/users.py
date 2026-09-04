"""平台主体、认证身份、本地凭据与会话版本的 PostgreSQL 仓储。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.auth.accounts import (
    AccountNotFound,
    AccountSourceConflict,
    LocalAccountRecord,
    PlatformAccount,
)
from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    SessionStateUnavailable,
)
from app.core.auth.identity import normalize_login_name, validate_local_login_name
from app.core.auth.roles import ExistingUser, Role, RoleResolver
from app.core.runtime_resources import (
    bind_connection_audit_subject,
    bind_connection_system_audit,
    database_engine,
)
from app.services.admin_invariant import ensure_effective_admin, lock_admin_invariant
from app.settings import Settings, get_settings

LOCAL_ACCOUNT_SELECT = """
SELECT
  ua.id AS account_id,
  ai.id AS identity_id,
  ap.code AS provider_code,
  ai.login_name,
  ai.normalized_login_name,
  ua.display_name,
  ua.dept,
  ua.role,
  ua.security_version,
  ua.status AS account_status,
  ai.status AS identity_status,
  ap.enabled AS provider_enabled,
  lc.must_change_password,
  lc.password_hash,
  lc.credential_version
FROM user_account ua
JOIN auth_identity ai ON ai.account_id=ua.id
JOIN auth_provider ap ON ap.id=ai.provider_id
JOIN local_credential lc ON lc.identity_id=ai.id
"""

SECURITY_SESSION_SELECT = """
SELECT
  ua.id AS account_id,
  ai.id AS identity_id,
  ap.code AS provider_code,
  ai.login_name,
  ai.normalized_login_name,
  ua.display_name,
  ua.dept,
  ua.role,
  ua.security_version,
  ua.status AS account_status,
  ai.status AS identity_status,
  ap.enabled AS provider_enabled
FROM user_account ua
JOIN auth_identity ai ON ai.account_id=ua.id
JOIN auth_provider ap ON ap.id=ai.provider_id
"""


class PasswordChangeInProgress(RuntimeError):
    """同一首次改密令牌已被另一个未过期租约占用。"""


class AuthContextChanged(RuntimeError):
    """管理员处置或并发改密已使安全/凭据版本失效。"""


@dataclass(frozen=True, slots=True)
class PasswordChangeAuthorization:
    """日常改密重新认证后可提交的安全快照。"""

    account_id: int
    identity_id: int
    provider_code: Literal["local"]
    normalized_login_name: str
    expected_security_version: int
    expected_credential_version: int


@dataclass(frozen=True, slots=True)
class PasswordChangeClaim:
    """首次改密高成本计算前取得的数据库 fencing 租约。"""

    token_id: int
    lease_id: UUID
    lease_expires_at: datetime
    current_password_hash: str
    issued_credential_version: int = 1


def _security_account(row: Any) -> PlatformAccount:
    return PlatformAccount(
        account_id=int(row["account_id"]),
        identity_id=int(row["identity_id"]),
        provider_code=str(row["provider_code"]),
        login_name=str(row["login_name"]),
        normalized_login_name=str(row["normalized_login_name"]),
        display_name=str(row["display_name"]),
        dept=str(row["dept"]),
        role=cast(Role, row["role"]),
        security_version=int(row["security_version"]),
        account_enabled=int(row["account_status"]) == 1,
        identity_enabled=int(row["identity_status"]) == 1,
        provider_enabled=bool(row["provider_enabled"]),
    )


def _local_record(row: Any) -> LocalAccountRecord:
    return LocalAccountRecord(
        account=PlatformAccount(
            account_id=int(row["account_id"]),
            identity_id=int(row["identity_id"]),
            provider_code=str(row["provider_code"]),
            login_name=str(row["login_name"]),
            normalized_login_name=str(row["normalized_login_name"]),
            display_name=str(row["display_name"]),
            dept=str(row["dept"]),
            role=cast(Role, row["role"]),
            security_version=int(row["security_version"]),
            account_enabled=int(row["account_status"]) == 1,
            identity_enabled=int(row["identity_status"]) == 1,
            provider_enabled=bool(row["provider_enabled"]),
            must_change_password=bool(row["must_change_password"]),
        ),
        password_hash=str(row["password_hash"]),
        credential_version=int(row["credential_version"]),
    )


class UserRepository(Protocol):
    async def find_local_account(
        self,
        normalized_login_name: str,
    ) -> LocalAccountRecord | None: ...

    async def load_security_session(
        self,
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount: ...

    async def resolve_identity(
        self,
        identity: AuthenticatedIdentity,
        ip: str,
    ) -> PlatformAccount: ...

    async def find_local_account_by_id(
        self,
        account_id: int,
    ) -> LocalAccountRecord | None: ...

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
    ) -> None: ...

    async def claim_password_change_token(
        self,
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
    ) -> PasswordChangeClaim: ...

    async def release_password_change_token(
        self,
        *,
        token_id: int,
        lease_id: UUID,
    ) -> bool: ...

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
    ) -> None: ...

    async def change_local_password(
        self,
        *,
        account_id: int,
        identity_id: int,
        password_hash: str,
        actor: str,
        ip: str,
        expected_security_version: int,
        expected_credential_version: int,
    ) -> None: ...

    async def invalidate_sessions(
        self,
        actor: PlatformAccount,
        account_id: int,
        ip: str,
    ) -> None: ...

    async def audit_logout(self, actor: PlatformAccount, ip: str) -> None: ...

    async def audit_refresh(self, actor: PlatformAccount, ip: str) -> None: ...


class SqlUserRepository:
    """账号写入、版本失效与无敏感审计均使用稳定 account_id。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("auth"))

    async def find_local_account(
        self,
        normalized_login_name: str,
    ) -> LocalAccountRecord | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        {LOCAL_ACCOUNT_SELECT}
                        WHERE ap.code='local'
                          AND ai.normalized_login_name=:normalized_login_name
                        """
                    ),
                    {"normalized_login_name": normalized_login_name},
                )
                row = result.mappings().one_or_none()
                return _local_record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def find_local_account_by_id(
        self,
        account_id: int,
    ) -> LocalAccountRecord | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        {LOCAL_ACCOUNT_SELECT}
                        WHERE ap.code='local' AND ua.id=:account_id
                        """
                    ),
                    {"account_id": account_id},
                )
                row = result.mappings().one_or_none()
                return _local_record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def resolve_identity(
        self,
        identity: AuthenticatedIdentity,
        ip: str,
    ) -> PlatformAccount:
        """把 Provider 结果解析为稳定主体；来源冲突由全局唯一名裁决。"""

        if identity.account is not None:
            await self._record_local_login(identity.account, ip)
            return identity.account
        return await self._synchronize_external(identity, ip)

    async def _record_local_login(self, account: PlatformAccount, ip: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE user_account SET last_login_at=now(),updated_at=now()
                        WHERE id=:account_id AND status=1
                        """
                    ),
                    {"account_id": account.account_id},
                )
                await self._audit_login(connection, account, ip)
        finally:
            await engine.dispose()

    async def _synchronize_external(
        self,
        identity: AuthenticatedIdentity,
        ip: str,
    ) -> PlatformAccount:
        normalized = normalize_login_name(identity.login_name)
        if not normalized or len(normalized) > 64 or not identity.external_subject:
            raise InvalidCredentials("目录身份缺少平台所需属性")
        engine = self._engine()
        try:
            try:
                async with engine.begin() as connection:
                    await lock_admin_invariant(connection)
                    provider_result = await connection.execute(
                        text(
                            """
                            SELECT id FROM auth_provider
                            WHERE code=:provider_code AND enabled=TRUE FOR SHARE
                            """
                        ),
                        {"provider_code": identity.provider_code},
                    )
                    provider_id = provider_result.scalar_one_or_none()
                    if provider_id is None:
                        raise InvalidCredentials("认证源未启用")
                    current_result = await connection.execute(
                        text(
                            """
                            SELECT
                              ua.id AS account_id,ai.id AS identity_id,
                              ua.role,ua.role_override,ua.security_version,
                              ua.status AS account_status,ai.status AS identity_status
                            FROM auth_identity ai
                            JOIN user_account ua ON ua.id=ai.account_id
                            WHERE ai.provider_id=:provider_id
                              AND ai.external_subject=:external_subject
                            FOR UPDATE
                            """
                        ),
                        {
                            "provider_id": int(provider_id),
                            "external_subject": identity.external_subject,
                        },
                    )
                    current = current_result.mappings().one_or_none()
                    if current is None:
                        conflict = await connection.execute(
                            text(
                                """
                                SELECT id FROM auth_identity
                                WHERE normalized_login_name=:normalized_login_name
                                FOR UPDATE
                                """
                            ),
                            {"normalized_login_name": normalized},
                        )
                        if conflict.scalar_one_or_none() is not None:
                            raise AccountSourceConflict("登录名已被其他认证身份占用")
                    elif (
                        int(current["account_status"]) != 1 or int(current["identity_status"]) != 1
                    ):
                        raise InvalidCredentials("用户名或密码错误")
                    mapping_result = await connection.execute(
                        text(
                            """
                            SELECT external_group,role,dept FROM external_role_mapping
                            WHERE provider_id=:provider_id
                              AND external_group=ANY(CAST(:groups AS text[]))
                            """
                        ),
                        {
                            "provider_id": int(provider_id),
                            "groups": list(identity.groups),
                        },
                    )
                    mapping_rows = list(mapping_result.mappings())
                    mappings = {
                        str(row["external_group"]): str(row["role"])
                        for row in mapping_rows
                    }
                    mapped_departments = {
                        str(row["dept"]).strip()
                        for row in mapping_rows
                        if row["dept"] is not None and str(row["dept"]).strip()
                    }
                    if len(mapped_departments) != 1:
                        raise InvalidCredentials("目录组未映射到唯一授权部门")
                    mapped_dept = mapped_departments.pop()
                    existing = (
                        ExistingUser(
                            role=cast(Role, current["role"]),
                            role_override=bool(current["role_override"]),
                        )
                        if current is not None
                        else None
                    )
                    role = RoleResolver().resolve(identity, existing, mappings).role
                    if current is None:
                        account_result = await connection.execute(
                            text(
                                """
                                INSERT INTO user_account(
                                  display_name,dept,role,role_override,status,last_login_at
                                ) VALUES(
                                  :display_name,:dept,:role,FALSE,1,now()
                                ) RETURNING id
                                """
                            ),
                            {
                                "display_name": identity.display_name,
                                "dept": mapped_dept,
                                "role": role,
                            },
                        )
                        created = account_result.mappings().one()
                        account_id = int(created["id"])
                        identity_result = await connection.execute(
                            text(
                                """
                                INSERT INTO auth_identity(
                                  account_id,provider_id,login_name,
                                  normalized_login_name,external_subject,status,
                                  source_groups,last_synced_at
                                ) VALUES(
                                  :account_id,:provider_id,:login_name,
                                  :normalized_login_name,:external_subject,1,
                                  CAST(:source_groups AS text[]),now()
                                ) RETURNING id
                                """
                            ),
                            {
                                "account_id": account_id,
                                "provider_id": int(provider_id),
                                "login_name": normalized,
                                "normalized_login_name": normalized,
                                "external_subject": identity.external_subject,
                                "source_groups": list(identity.groups),
                            },
                        )
                        identity_id = int(identity_result.scalar_one())
                    else:
                        account_id = int(current["account_id"])
                        identity_id = int(current["identity_id"])
                        await connection.execute(
                            text(
                                """
                                UPDATE user_account SET
                                  display_name=CAST(:display_name AS varchar(128)),
                                  dept=CAST(:dept AS varchar(128)),
                                  security_version=security_version+CASE
                                    WHEN role IS DISTINCT FROM
                                           CAST(:role AS varchar(16))
                                      OR dept IS DISTINCT FROM
                                           CAST(:dept AS varchar(128))
                                    THEN 1 ELSE 0 END,
                                  role=CAST(:role AS varchar(16)),
                                  last_login_at=now(),updated_at=now()
                                WHERE id=:account_id
                                """
                            ),
                            {
                                "account_id": account_id,
                                "display_name": identity.display_name,
                                "dept": mapped_dept,
                                "role": role,
                            },
                        )
                        await connection.execute(
                            text(
                                """
                                UPDATE auth_identity SET
                                  login_name=:login_name,
                                  normalized_login_name=:normalized_login_name,
                                  source_groups=CAST(:source_groups AS text[]),
                                  last_synced_at=now(),updated_at=now()
                                WHERE id=:identity_id
                                """
                            ),
                            {
                                "identity_id": identity_id,
                                "login_name": normalized,
                                "normalized_login_name": normalized,
                                "source_groups": list(identity.groups),
                            },
                        )
                        await ensure_effective_admin(connection)
                    authoritative = await connection.execute(
                        text(
                            f"""
                            {SECURITY_SESSION_SELECT}
                            WHERE ua.id=:account_id AND ai.id=:identity_id
                            """
                        ),
                        {"account_id": account_id, "identity_id": identity_id},
                    )
                    session = authoritative.mappings().one()
                    account = _security_account(session)
                    await self._audit_login(connection, account, ip)
                    return account
            except (AccountSourceConflict, IntegrityError):
                async with engine.begin() as audit_connection:
                    await bind_connection_system_audit(
                        audit_connection,
                        actor_name="auth-system",
                        action="account_source_conflict",
                    )
                    await audit_connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(
                              actor,actor_subject_kind,role,ip,action,object_type,
                              object_id,after_val
                            ) VALUES(
                              'auth-system','system',NULL,CAST(:ip AS inet),
                              'account_source_conflict',
                              'auth_identity',:object_id,
                              jsonb_build_object(
                                'provider_code',CAST(:provider_code AS text),
                                'result_code','ACCOUNT_SOURCE_CONFLICT'
                              )
                            )
                            """
                        ),
                        {
                            "ip": ip,
                            "object_id": normalized,
                            "provider_code": identity.provider_code,
                        },
                    )
                raise AccountSourceConflict("登录名已被其他认证身份占用") from None
        finally:
            await engine.dispose()

    @staticmethod
    async def _audit_login(connection: Any, account: PlatformAccount, ip: str) -> None:
        await bind_connection_audit_subject(
            connection,
            subject_kind="human",
            actor_name=account.login_name,
            account_id=account.account_id,
            identity_id=account.identity_id,
        )
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(
                  actor,actor_subject_kind,actor_account_id,actor_identity_id,
                  role,ip,action,object_type,object_id,after_val
                ) VALUES(
                  :actor,'human',:actor_account_id,:actor_identity_id,
                  CAST(:role AS text),CAST(:ip AS inet),'login',
                  'user_account',:object_id,
                  jsonb_build_object(
                    'provider_code',CAST(:provider_code AS text),
                    'role',CAST(:role AS text)
                  )
                )
                """
            ),
            {
                "actor": account.login_name,
                "actor_account_id": account.account_id,
                "actor_identity_id": account.identity_id,
                "role": account.role,
                "ip": ip,
                "object_id": str(account.account_id),
                "provider_code": account.provider_code,
            },
        )

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
        """只持久化令牌指纹，并绑定签发时的本地主体安全上下文。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                inserted = await connection.execute(
                    text(
                        """
                        INSERT INTO password_change_token(
                          token_hash,account_id,identity_id,provider_code,purpose,
                          normalized_login_name,issued_security_version,
                          issued_credential_version,status,expires_at
                        )
                        SELECT
                          :token_hash,ua.id,ai.id,ap.code,'initial_password',
                          ai.normalized_login_name,ua.security_version,
                          lc.credential_version,'available',:expires_at
                        FROM user_account ua
                        JOIN auth_identity ai ON ai.account_id=ua.id
                        JOIN auth_provider ap ON ap.id=ai.provider_id
                        JOIN local_credential lc ON lc.identity_id=ai.id
                        WHERE ua.id=:account_id
                          AND ai.id=:identity_id
                          AND ap.code=:provider_code
                          AND ai.normalized_login_name=:login_name
                          AND ua.security_version=:security_version
                          AND ua.status=1 AND ai.status=1 AND ap.enabled=TRUE
                          AND lc.must_change_password=TRUE
                        RETURNING id
                        """
                    ),
                    {
                        "token_hash": token_hash,
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "provider_code": provider_code,
                        "login_name": login_name,
                        "security_version": security_version,
                        "expires_at": expires_at,
                    },
                )
                if inserted.scalar_one_or_none() is None:
                    raise InvalidCredentials("本地改密上下文已失效")
        finally:
            await engine.dispose()

    async def claim_password_change_token(
        self,
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
    ) -> PasswordChangeClaim:
        """短事务取得首次改密租约；昂贵 Argon2 不在事务内执行。"""

        lease_id = uuid4()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                selected = await connection.execute(
                    text(
                        """
                        SELECT pct.id,pct.status,
                               pct.processing_lease_expires_at,
                               pct.processing_lease_expires_at>now() AS lease_active,
                               lc.password_hash
                        FROM password_change_token pct
                        JOIN user_account ua ON ua.id=pct.account_id
                        JOIN auth_identity ai
                          ON ai.id=pct.identity_id AND ai.account_id=ua.id
                        JOIN auth_provider ap ON ap.id=ai.provider_id
                        JOIN local_credential lc ON lc.identity_id=ai.id
                        WHERE pct.token_hash=:token_hash
                          AND pct.account_id=:account_id
                          AND pct.identity_id=:identity_id
                          AND pct.provider_code=:provider_code
                          AND pct.purpose='initial_password'
                          AND pct.normalized_login_name=:login_name
                          AND pct.expires_at>now()
                          AND pct.issued_security_version=ua.security_version
                          AND pct.issued_credential_version=lc.credential_version
                          AND ua.status=1 AND ai.status=1 AND ap.enabled=TRUE
                          AND ap.code=:provider_code
                          AND ai.normalized_login_name=:login_name
                          AND lc.must_change_password=TRUE
                        FOR UPDATE OF pct,ua
                        """
                    ),
                    {
                        "token_hash": token_hash,
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "provider_code": provider_code,
                        "login_name": login_name,
                    },
                )
                row = selected.mappings().one_or_none()
                if row is None:
                    raise InvalidCredentials("改密令牌无效、已过期或已使用")
                status = str(row["status"])
                if status == "processing" and bool(row["lease_active"]):
                    raise PasswordChangeInProgress("改密请求处理中")
                if status not in {"available", "processing"}:
                    raise InvalidCredentials("改密令牌无效、已过期或已使用")
                claimed = await connection.execute(
                    text(
                        """
                        UPDATE password_change_token SET
                          status='processing',
                          processing_lease_id=:lease_id,
                          processing_lease_expires_at=now()+INTERVAL '30 seconds'
                        WHERE id=:token_id
                        RETURNING processing_lease_expires_at
                        """
                    ),
                    {"token_id": int(row["id"]), "lease_id": lease_id},
                )
                lease_expires_at = claimed.scalar_one()
                return PasswordChangeClaim(
                    token_id=int(row["id"]),
                    lease_id=lease_id,
                    lease_expires_at=lease_expires_at,
                    current_password_hash=str(row["password_hash"]),
                )
        except SQLAlchemyError as error:
            raise SessionStateUnavailable("改密令牌状态暂不可用") from error
        finally:
            await engine.dispose()

    async def release_password_change_token(
        self,
        *,
        token_id: int,
        lease_id: UUID,
    ) -> bool:
        """仅持有当前 fencing UUID 的请求可以释放首次改密租约。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                released = await connection.execute(
                    text(
                        """
                        UPDATE password_change_token SET
                          status='available',
                          processing_lease_id=NULL,
                          processing_lease_expires_at=NULL
                        WHERE id=:token_id
                          AND status='processing'
                          AND processing_lease_id=:lease_id
                        RETURNING id
                        """
                    ),
                    {"token_id": token_id, "lease_id": lease_id},
                )
                return released.scalar_one_or_none() is not None
        except SQLAlchemyError as error:
            raise SessionStateUnavailable("改密令牌状态暂不可用") from error
        finally:
            await engine.dispose()

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
        """在一个 PostgreSQL 事务内消费首次改密令牌、改密、撤权并审计。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                locked = await connection.execute(
                    text(
                        """
                        SELECT pct.id
                        FROM password_change_token pct
                        JOIN user_account ua ON ua.id=pct.account_id
                        JOIN auth_identity ai
                          ON ai.id=pct.identity_id AND ai.account_id=ua.id
                        JOIN auth_provider ap ON ap.id=ai.provider_id
                        JOIN local_credential lc ON lc.identity_id=ai.id
                        WHERE pct.id=:token_id
                          AND pct.account_id=:account_id
                          AND pct.identity_id=:identity_id
                          AND pct.provider_code=:provider_code
                          AND pct.purpose='initial_password'
                          AND pct.normalized_login_name=:login_name
                          AND pct.status='processing'
                          AND pct.processing_lease_id=:lease_id
                          AND pct.processing_lease_expires_at>now()
                          AND pct.expires_at>now()
                          AND pct.issued_security_version=ua.security_version
                          AND pct.issued_credential_version=lc.credential_version
                          AND ua.status=1 AND ai.status=1 AND ap.enabled=TRUE
                          AND ap.code=:provider_code
                          AND ai.normalized_login_name=:login_name
                          AND lc.must_change_password=TRUE
                        FOR UPDATE OF pct,ua
                        """
                    ),
                    {
                        "token_id": token_id,
                        "lease_id": lease_id,
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "provider_code": provider_code,
                        "login_name": login_name,
                    },
                )
                token_id = locked.scalar_one_or_none()
                if token_id is None:
                    raise InvalidCredentials("改密令牌无效、已过期或已使用")
                updated = await connection.execute(
                    text(
                        """
                        UPDATE local_credential SET
                          password_hash=:password_hash,
                          must_change_password=FALSE,
                          credential_version=credential_version+1,
                          password_changed_at=now(),updated_at=now()
                        WHERE identity_id=:identity_id
                          AND must_change_password=TRUE
                        RETURNING identity_id
                        """
                    ),
                    {
                        "identity_id": identity_id,
                        "password_hash": password_hash,
                    },
                )
                if updated.scalar_one_or_none() is None:
                    raise InvalidCredentials("本地改密上下文已失效")
                await connection.execute(
                    text(
                        """
                        UPDATE user_account SET security_version=security_version+1,
                          updated_at=now() WHERE id=:account_id
                        """
                    ),
                    {"account_id": account_id},
                )
                await connection.execute(
                    text(
                        """
                        UPDATE password_change_token SET
                          status=CASE WHEN id=:token_id THEN 'consumed' ELSE 'revoked' END,
                          consumed_at=CASE WHEN id=:token_id THEN now() ELSE NULL END,
                          processing_lease_id=NULL,
                          processing_lease_expires_at=NULL
                        WHERE account_id=:account_id
                          AND status IN ('available','processing')
                        """
                    ),
                    {"token_id": int(token_id), "account_id": account_id},
                )
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=actor,
                    account_id=account_id,
                    identity_id=identity_id,
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'human',:account_id,:identity_id,
                          NULL,CAST(:ip AS inet),'local_password_change',
                          'user_account',:object_id,
                          jsonb_build_object('provider_code','local')
                        )
                        """
                    ),
                    {
                        "actor": actor,
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "ip": ip,
                        "object_id": str(account_id),
                    },
                )
        except SQLAlchemyError as error:
            raise SessionStateUnavailable("改密事务状态暂不可用") from error
        finally:
            await engine.dispose()

    async def change_local_password(
        self,
        *,
        account_id: int,
        identity_id: int,
        password_hash: str,
        actor: str,
        ip: str,
        expected_security_version: int,
        expected_credential_version: int,
    ) -> None:
        """短事务 CAS 安全版本与凭据版本；Argon2 不得在本锁内执行。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                cas = await connection.execute(
                    text(
                        """
                        UPDATE user_account ua
                        SET security_version = ua.security_version + 1,
                            updated_at = now()
                        WHERE ua.id = :account_id
                          AND ua.status = 1
                          AND ua.security_version = :expected_security_version
                          AND EXISTS (
                            SELECT 1
                            FROM auth_identity ai
                            JOIN auth_provider ap ON ap.id = ai.provider_id
                            WHERE ai.id = :identity_id
                              AND ai.account_id = ua.id
                              AND ai.status = 1
                              AND ap.code = 'local'
                              AND ap.enabled = TRUE
                          )
                        RETURNING ua.id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "expected_security_version": expected_security_version,
                    },
                )
                if cas.scalar_one_or_none() is None:
                    raise AuthContextChanged("账号安全状态已变化")
                updated = await connection.execute(
                    text(
                        """
                        UPDATE local_credential
                        SET password_hash = :password_hash,
                            must_change_password = FALSE,
                            credential_version = credential_version + 1,
                            password_changed_at = now(),
                            updated_at = now()
                        WHERE identity_id = :identity_id
                          AND credential_version = :expected_credential_version
                          AND must_change_password = FALSE
                        RETURNING identity_id
                        """
                    ),
                    {
                        "identity_id": identity_id,
                        "password_hash": password_hash,
                        "expected_credential_version": expected_credential_version,
                    },
                )
                if updated.scalar_one_or_none() is None:
                    raise AuthContextChanged("账号安全状态已变化")
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=actor,
                    account_id=account_id,
                    identity_id=identity_id,
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'human',:account_id,:identity_id,
                          NULL,CAST(:ip AS inet),'local_password_change',
                          'user_account',:object_id,
                          jsonb_build_object('provider_code','local')
                        )
                        """
                    ),
                    {
                        "actor": actor,
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "ip": ip,
                        "object_id": str(account_id),
                    },
                )
        finally:
            await engine.dispose()

    async def create_local_account(
        self,
        *,
        login_name: str,
        display_name: str,
        dept: str,
        role: Role,
        password_hash: str,
        actor: str,
        ip: str,
    ) -> PlatformAccount:
        """事务创建主体、local 身份与临时凭据；唯一约束裁决先到先得。"""

        normalized = validate_local_login_name(login_name)
        engine = self._engine()
        try:
            try:
                async with engine.begin() as connection:
                    provider_result = await connection.execute(
                        text(
                            """
                            SELECT id FROM auth_provider
                            WHERE code='local' AND enabled=TRUE FOR SHARE
                            """
                        )
                    )
                    provider_id = provider_result.scalar_one_or_none()
                    if provider_id is None:
                        raise RuntimeError("内置本地认证源不可用")
                    account_result = await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(display_name,dept,role,role_override,status)
                            VALUES(:display_name,:dept,:role,TRUE,1)
                            RETURNING id
                            """
                        ),
                        {
                            "display_name": display_name.strip(),
                            "dept": dept.strip(),
                            "role": role,
                        },
                    )
                    account_id = int(account_result.scalar_one())
                    identity_result = await connection.execute(
                        text(
                            """
                            INSERT INTO auth_identity(
                              account_id,provider_id,login_name,normalized_login_name,
                              external_subject,status
                            ) VALUES(
                              :account_id,:provider_id,:login_name,:normalized_login_name,
                              :external_subject,1
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "account_id": account_id,
                            "provider_id": int(provider_id),
                            "login_name": normalized,
                            "normalized_login_name": normalized,
                            "external_subject": f"local:{normalized}",
                        },
                    )
                    identity_id = int(identity_result.scalar_one())
                    await connection.execute(
                        text(
                            """
                            INSERT INTO local_credential(
                              identity_id,password_hash,must_change_password
                            ) VALUES(:identity_id,:password_hash,TRUE)
                            """
                        ),
                        {
                            "identity_id": identity_id,
                            "password_hash": password_hash,
                        },
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(
                              actor,role,ip,action,object_type,object_id,after_val
                            ) VALUES(
                              :actor,'admin',CAST(:ip AS inet),'local_account_create',
                              'user_account',:object_id,
                              jsonb_build_object(
                                'provider_code','local',
                                'role',CAST(:target_role AS text)
                              )
                            )
                            """
                        ),
                        {
                            "actor": actor,
                            "ip": ip,
                            "object_id": str(account_id),
                            "target_role": role,
                        },
                    )
                    created = await connection.execute(
                        text(
                            f"""
                            {LOCAL_ACCOUNT_SELECT}
                            WHERE ua.id=:account_id AND ai.id=:identity_id
                            """
                        ),
                        {"account_id": account_id, "identity_id": identity_id},
                    )
                    row = created.mappings().one_or_none()
                    if row is None:
                        raise RuntimeError("本地账号创建后无法读取")
                    return _local_record(row).account
            except IntegrityError:
                raise AccountSourceConflict("登录名已被其他认证身份占用") from None
        finally:
            await engine.dispose()

    async def load_security_session(
        self,
        account_id: int,
        identity_id: int,
    ) -> PlatformAccount:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        {SECURITY_SESSION_SELECT}
                        WHERE ua.id=:account_id AND ai.id=:identity_id
                        """
                    ),
                    {"account_id": account_id, "identity_id": identity_id},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise InvalidCredentials("无效或已吊销的令牌")
                return _security_account(row)
        finally:
            await engine.dispose()

    async def invalidate_sessions(
        self,
        actor: PlatformAccount,
        account_id: int,
        ip: str,
    ) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        """
                        UPDATE user_account SET security_version = security_version + 1,
                          updated_at=now() WHERE id=:account_id RETURNING id
                        """
                    ),
                    {"account_id": account_id},
                )
                if updated.scalar_one_or_none() is None:
                    raise AccountNotFound(account_id)
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id
                        ) VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :role,CAST(:ip AS inet),'force_logout',
                          'user_account',:object_id
                        )
                        """
                    ),
                    {
                        "actor": actor.login_name,
                        "actor_account_id": actor.account_id,
                        "actor_identity_id": actor.identity_id,
                        "role": actor.role,
                        "ip": ip,
                        "object_id": str(account_id),
                    },
                )
        finally:
            await engine.dispose()

    async def audit_logout(self, actor: PlatformAccount, ip: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id
                        ) VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :role,CAST(:ip AS inet),'logout',
                          'user_account',:object_id
                        )
                        """
                    ),
                    {
                        "actor": actor.login_name,
                        "actor_account_id": actor.account_id,
                        "actor_identity_id": actor.identity_id,
                        "role": actor.role,
                        "ip": ip,
                        "object_id": str(actor.account_id),
                    },
                )
        finally:
            await engine.dispose()

    async def audit_refresh(self, actor: PlatformAccount, ip: str) -> None:
        """持久化成功 refresh 事件；失败必须由调用方吊销新会话。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id
                        ) VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :role,CAST(:ip AS inet),'session_refresh',
                          'user_account',:object_id
                        )
                        """
                    ),
                    {
                        "actor": actor.login_name,
                        "actor_account_id": actor.account_id,
                        "actor_identity_id": actor.identity_id,
                        "role": actor.role,
                        "ip": ip,
                        "object_id": str(actor.account_id),
                    },
                )
        finally:
            await engine.dispose()


# 兼容重构期间的模块导入；调用方必须迁移到稳定主体语义。
PlatformUser = PlatformAccount
