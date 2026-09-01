"""管理员维护平台主体、本地凭据和外部角色跟随的应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.core.auth.backends import ProviderCapacityUnavailable
from app.core.auth.identity import validate_local_login_name
from app.core.auth.passwords import LocalPasswordHasher, PasswordPolicy
from app.core.auth.roles import Role
from app.core.bounded_executor import ExecutorBackpressure, run_bounded

SyncStatus = Literal["local", "synced", "pending", "disabled"]
CredentialStatus = Literal["active", "must_change"]


class UserNotFound(LookupError):
    """目标平台主体不存在。"""


class RoleMappingConflict(RuntimeError):
    """外部来源组无法按当前映射恢复角色，或本地身份试图跟随目录。"""


class ProviderActionUnsupported(RuntimeError):
    """目标身份的认证源不支持该操作。"""


class SelfDisableDenied(RuntimeError):
    """管理员不能禁用自己。"""


class LastAdminProtected(RuntimeError):
    """不能禁用或降级最后一个有效管理员。"""


@dataclass(frozen=True, slots=True)
class UserRecord:
    account_id: int
    identity_id: int
    provider_code: str
    username: str
    display_name: str
    dept: str
    role: Role
    role_override: bool
    status: int
    identity_status: int
    must_change_password: bool | None
    source_groups: tuple[str, ...]
    last_synced_at: datetime | None
    last_login_at: datetime | None
    security_version: int = 1

    @property
    def sync_status(self) -> SyncStatus:
        if self.status != 1 or self.identity_status != 1:
            return "disabled"
        if self.provider_code == "local":
            return "local"
        return "synced" if self.last_synced_at is not None else "pending"

    @property
    def credential_status(self) -> CredentialStatus | None:
        if self.provider_code != "local":
            return None
        return "must_change" if self.must_change_password else "active"


@dataclass(frozen=True, slots=True)
class UserQuery:
    keyword: str | None
    provider_code: str | None
    role: Role | None
    status: int | None
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class UserPage:
    items: tuple[UserRecord, ...]
    total: int
    page: int
    page_size: int


class UserManagementRepository(Protocol):
    async def list(self, query: UserQuery) -> UserPage: ...

    async def get(self, account_id: int) -> UserRecord: ...

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
    ) -> UserRecord: ...

    async def set_role(
        self,
        account_id: int,
        role: Role,
        role_override: bool,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord: ...

    async def set_status(
        self,
        account_id: int,
        status: int,
        *,
        actor_account_id: int,
        actor: str,
        ip: str,
    ) -> UserRecord: ...

    async def reset_local_password(
        self,
        account_id: int,
        password_hash: str,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord: ...


class PasswordEncoder(Protocol):
    def hash(self, password: str) -> str: ...


class UserManagementService:
    """校验管理员意图，并把安全敏感变更委托给事务仓储。"""

    def __init__(
        self,
        repository: UserManagementRepository,
        passwords: PasswordEncoder | None = None,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.passwords = passwords or LocalPasswordHasher()
        self.policy = policy or PasswordPolicy()

    async def list(
        self,
        keyword: str | None,
        provider_code: str | None,
        role: Role | None,
        status: int | None,
        page: int,
        page_size: int,
    ) -> UserPage:
        normalized_keyword = keyword.strip() if keyword and keyword.strip() else None
        normalized_provider = (
            provider_code.strip().casefold() if provider_code and provider_code.strip() else None
        )
        return await self.repository.list(
            UserQuery(
                normalized_keyword,
                normalized_provider,
                role,
                status,
                page,
                page_size,
            )
        )

    async def create_local(
        self,
        *,
        username: str,
        display_name: str,
        dept: str,
        role: Role,
        temporary_password: str,
        actor: str,
        ip: str,
    ) -> UserRecord:
        normalized = validate_local_login_name(username)
        self.policy.validate(temporary_password, username=normalized)
        try:
            password_hash = await run_bounded(
                self.passwords.hash,
                temporary_password,
                timeout_s=5,
                pool="auth_hash",
            )
        except (ExecutorBackpressure, TimeoutError):
            raise ProviderCapacityUnavailable("本地认证容量暂不可用") from None
        return await self.repository.create_local(
            username=normalized,
            display_name=display_name.strip(),
            dept=dept.strip(),
            role=role,
            password_hash=password_hash,
            actor=actor,
            ip=ip,
        )

    async def change_role(
        self,
        account_id: int,
        role: Role,
        role_override: bool,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        return await self.repository.set_role(
            account_id,
            role,
            role_override,
            actor=actor,
            ip=ip,
        )

    async def change_status(
        self,
        account_id: int,
        status: int,
        *,
        actor_account_id: int,
        actor: str,
        ip: str,
    ) -> UserRecord:
        if status == 0 and account_id == actor_account_id:
            raise SelfDisableDenied("管理员不能禁用自己")
        return await self.repository.set_status(
            account_id,
            status,
            actor_account_id=actor_account_id,
            actor=actor,
            ip=ip,
        )

    async def reset_password(
        self,
        account_id: int,
        temporary_password: str,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        current = await self.repository.get(account_id)
        if current.provider_code != "local":
            raise ProviderActionUnsupported("仅本地账号支持密码重置")
        self.policy.validate(temporary_password, username=current.username)
        try:
            password_hash = await run_bounded(
                self.passwords.hash,
                temporary_password,
                timeout_s=5,
                pool="auth_hash",
            )
        except (ExecutorBackpressure, TimeoutError):
            raise ProviderCapacityUnavailable("本地认证容量暂不可用") from None
        return await self.repository.reset_local_password(
            account_id,
            password_hash,
            actor=actor,
            ip=ip,
        )
