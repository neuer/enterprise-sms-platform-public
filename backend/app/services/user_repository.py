"""管理员账号生命周期的 PostgreSQL 查询、锁保护与无敏感审计。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.auth.accounts import AccountSourceConflict
from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials
from app.core.auth.roles import ExistingUser, Role, RoleResolver
from app.core.runtime_resources import database_engine
from app.services.admin_invariant import ensure_effective_admin, lock_admin_invariant
from app.services.user_management import (
    ProviderActionUnsupported,
    RoleMappingConflict,
    SelfDisableDenied,
    UserNotFound,
    UserPage,
    UserQuery,
    UserRecord,
)
from app.settings import Settings, get_settings

USER_COLUMNS = """
ua.id AS account_id,
ai.id AS identity_id,
ap.id AS provider_id,
ap.code AS provider_code,
ai.login_name AS username,
ua.display_name,
ua.dept,
ua.role,
ua.role_override,
ua.status,
ai.status AS identity_status,
lc.must_change_password,
ai.source_groups,
ai.last_synced_at,
ua.last_login_at,
ua.security_version
"""
USER_FROM = """
FROM user_account ua
JOIN auth_identity ai ON ai.account_id=ua.id
JOIN auth_provider ap ON ap.id=ai.provider_id
LEFT JOIN local_credential lc ON lc.identity_id=ai.id
"""


def _record(row: Any) -> UserRecord:
    return UserRecord(
        account_id=int(row["account_id"]),
        identity_id=int(row["identity_id"]),
        provider_code=str(row["provider_code"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        dept=str(row["dept"]),
        role=cast(Role, str(row["role"])),
        role_override=bool(row["role_override"]),
        status=int(row["status"]),
        identity_status=int(row["identity_status"]),
        must_change_password=(
            bool(row["must_change_password"]) if row["must_change_password"] is not None else None
        ),
        source_groups=tuple(str(group) for group in (row["source_groups"] or ())),
        last_synced_at=row["last_synced_at"],
        last_login_at=row["last_login_at"],
        security_version=int(row["security_version"]),
    )


class SqlUserManagementRepository:
    """账号变更、最后管理员保护与审计均在同一数据库事务。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("auth"))

    async def list(self, query: UserQuery) -> UserPage:
        clauses: list[str] = []
        params: dict[str, object] = {
            "limit": query.page_size,
            "offset": (query.page - 1) * query.page_size,
        }
        if query.keyword is not None:
            clauses.append(
                "(ai.login_name ILIKE :keyword OR ua.display_name ILIKE :keyword "
                "OR ua.dept ILIKE :keyword)"
            )
            params["keyword"] = f"%{query.keyword}%"
        if query.provider_code is not None:
            clauses.append("ap.code=:provider_code")
            params["provider_code"] = query.provider_code
        if query.role is not None:
            clauses.append("ua.role=:role")
            params["role"] = query.role
        if query.status is not None:
            clauses.append("ua.status=:status")
            params["status"] = query.status
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text(f"SELECT count(*) {USER_FROM}{where}"),
                    params,
                )
                result = await connection.execute(
                    text(
                        f"""
                        SELECT {USER_COLUMNS} {USER_FROM}{where}
                        ORDER BY ua.updated_at DESC,ua.id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
                return UserPage(
                    tuple(_record(row) for row in result.mappings()),
                    int(count.scalar_one()),
                    query.page,
                    query.page_size,
                )
        finally:
            await engine.dispose()

    async def get(self, account_id: int) -> UserRecord:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        SELECT {USER_COLUMNS} {USER_FROM}
                        WHERE ua.id=:account_id
                        """
                    ),
                    {"account_id": account_id},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise UserNotFound(account_id)
                return _record(row)
        finally:
            await engine.dispose()

    async def create_local(
        self,
        *,
        username: str,
        display_name: str,
        dept: str,
        role: Role,
        password_hash: str,
        actor: str,
        ip: str,
    ) -> UserRecord:
        engine = self._engine()
        try:
            try:
                async with engine.begin() as connection:
                    provider = await connection.execute(
                        text(
                            """
                            SELECT id FROM auth_provider
                            WHERE code='local' AND enabled=TRUE FOR SHARE
                            """
                        )
                    )
                    provider_id = provider.scalar_one_or_none()
                    if provider_id is None:
                        raise RuntimeError("内置本地认证源不可用")
                    account_result = await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(
                              display_name,dept,role,role_override,status
                            ) VALUES(:display_name,:dept,:role,TRUE,1)
                            RETURNING id
                            """
                        ),
                        {"display_name": display_name, "dept": dept, "role": role},
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
                            ) RETURNING id
                            """
                        ),
                        {
                            "account_id": account_id,
                            "provider_id": int(provider_id),
                            "login_name": username,
                            "normalized_login_name": username,
                            "external_subject": f"local:{username}",
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
                    result = await connection.execute(
                        text(
                            f"""
                            SELECT {USER_COLUMNS} {USER_FROM}
                            WHERE ua.id=:account_id AND ai.id=:identity_id
                            """
                        ),
                        {"account_id": account_id, "identity_id": identity_id},
                    )
                    row = result.mappings().one_or_none()
                    if row is None:
                        raise RuntimeError("本地账号创建后无法读取")
                    return _record(row)
            except IntegrityError:
                raise AccountSourceConflict("登录名已被其他认证身份占用") from None
        finally:
            await engine.dispose()

    async def set_role(
        self,
        account_id: int,
        role: Role,
        role_override: bool,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_admin_invariant(connection)
                row = await self._locked(connection, account_id)
                current = _record(row)
                if current.provider_code == "local" and not role_override:
                    raise RoleMappingConflict("本地账号必须使用人工角色")
                target_role = role
                target_dept = current.dept
                if current.provider_code != "local" and not role_override:
                    mapping_result = await connection.execute(
                        text(
                            """
                            SELECT external_group,role,dept FROM external_role_mapping
                            WHERE provider_id=:provider_id
                              AND external_group=ANY(CAST(:groups AS text[]))
                            """
                        ),
                        {
                            "provider_id": int(row["provider_id"]),
                            "groups": list(current.source_groups),
                        },
                    )
                    mapping_rows = list(mapping_result.mappings())
                    mappings = {
                        str(item["external_group"]): str(item["role"])
                        for item in mapping_rows
                    }
                    mapped_departments = {
                        str(item["dept"]).strip()
                        for item in mapping_rows
                        if item["dept"] is not None and str(item["dept"]).strip()
                    }
                    if len(mapped_departments) != 1:
                        raise RoleMappingConflict("最近目录来源组未映射到唯一授权部门")
                    target_dept = mapped_departments.pop()
                    try:
                        target_role = (
                            RoleResolver()
                            .resolve(
                                AuthenticatedIdentity(
                                    provider_code=current.provider_code,
                                    login_name=current.username,
                                    external_subject="managed-existing-identity",
                                    display_name=current.display_name,
                                    dept=current.dept,
                                    groups=current.source_groups,
                                ),
                                ExistingUser(current.role, False),
                                mappings,
                            )
                            .role
                        )
                    except InvalidCredentials:
                        raise RoleMappingConflict("最近目录来源组无法按当前映射恢复角色") from None
                await connection.execute(
                    text(
                        """
                        UPDATE user_account SET
                          role=:role,role_override=:role_override,dept=:dept,
                          security_version=security_version+1,updated_at=now()
                        WHERE id=:account_id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "role": target_role,
                        "dept": target_dept,
                        "role_override": role_override,
                    },
                )
                await ensure_effective_admin(connection)
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,role,ip,action,object_type,object_id,
                          before_val,after_val
                        ) VALUES(
                          :actor,'admin',CAST(:ip AS inet),'role_override',
                          'user_account',:object_id,
                          jsonb_build_object(
                            'role',CAST(:before_role AS text),
                            'role_override',CAST(:before_override AS boolean)
                          ),
                          jsonb_build_object(
                            'role',CAST(:after_role AS text),
                            'role_override',CAST(:after_override AS boolean)
                          )
                        )
                        """
                    ),
                    {
                        "actor": actor,
                        "ip": ip,
                        "object_id": str(account_id),
                        "before_role": current.role,
                        "before_override": current.role_override,
                        "after_role": target_role,
                        "after_override": role_override,
                    },
                )
                return replace(
                    current,
                    role=target_role,
                    role_override=role_override,
                    security_version=current.security_version + 1,
                )
        finally:
            await engine.dispose()

    async def set_status(
        self,
        account_id: int,
        status: int,
        *,
        actor_account_id: int,
        actor: str,
        ip: str,
    ) -> UserRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_admin_invariant(connection)
                row = await self._locked(connection, account_id)
                current = _record(row)
                if status == 0 and account_id == actor_account_id:
                    raise SelfDisableDenied("管理员不能禁用自己")
                await connection.execute(
                    text(
                        """
                        UPDATE user_account SET status=:status,
                          security_version=security_version+1,updated_at=now()
                        WHERE id=:account_id
                        """
                    ),
                    {"account_id": account_id, "status": status},
                )
                await ensure_effective_admin(connection)
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,role,ip,action,object_type,object_id,
                          before_val,after_val
                        ) VALUES(
                          :actor,'admin',CAST(:ip AS inet),'account_status_change',
                          'user_account',:object_id,
                          jsonb_build_object('status',CAST(:before_status AS integer)),
                          jsonb_build_object('status',CAST(:after_status AS integer))
                        )
                        """
                    ),
                    {
                        "actor": actor,
                        "ip": ip,
                        "object_id": str(account_id),
                        "before_status": current.status,
                        "after_status": status,
                    },
                )
                return replace(
                    current,
                    status=status,
                    security_version=current.security_version + 1,
                )
        finally:
            await engine.dispose()

    async def reset_local_password(
        self,
        account_id: int,
        password_hash: str,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                row = await self._locked(connection, account_id)
                current = _record(row)
                if current.provider_code != "local":
                    raise ProviderActionUnsupported("仅本地账号支持密码重置")
                await connection.execute(
                    text(
                        """
                        UPDATE local_credential SET
                          password_hash=:password_hash,must_change_password=TRUE,
                          password_changed_at=NULL,updated_at=now()
                        WHERE identity_id=:identity_id
                        """
                    ),
                    {
                        "identity_id": current.identity_id,
                        "password_hash": password_hash,
                    },
                )
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
                        INSERT INTO audit_log(
                          actor,role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'admin',CAST(:ip AS inet),'local_password_reset',
                          'user_account',:object_id,
                          jsonb_build_object(
                            'provider_code','local','must_change_password',TRUE
                          )
                        )
                        """
                    ),
                    {"actor": actor, "ip": ip, "object_id": str(account_id)},
                )
                return replace(
                    current,
                    must_change_password=True,
                    security_version=current.security_version + 1,
                )
        finally:
            await engine.dispose()

    async def _locked(self, connection: Any, account_id: int) -> Any:
        result = await connection.execute(
            text(
                f"""
                SELECT {USER_COLUMNS} {USER_FROM}
                WHERE ua.id=:account_id FOR UPDATE OF ua
                """
            ),
            {"account_id": account_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise UserNotFound(account_id)
        return row
