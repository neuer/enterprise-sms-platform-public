"""本地密码认证 Provider。"""

from __future__ import annotations

from typing import Literal, Protocol

from app.core.auth.accounts import LocalAccountRecord
from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    ProviderUnavailable,
)
from app.core.auth.department import valid_account_department
from app.core.auth.identity import normalize_login_name
from app.core.bounded_executor import ExecutorBackpressure, run_bounded

LocalHashPool = Literal["auth_login_hash", "auth_hash"]


class LocalAccountReader(Protocol):
    async def find_local_account(
        self,
        normalized_login_name: str,
    ) -> LocalAccountRecord | None: ...

    async def upgrade_local_password_hash(
        self, *, expected: LocalAccountRecord, password_hash: str,
    ) -> bool: ...


class PasswordVerifier(Protocol):
    def verify_or_dummy(self, encoded: str | None, password: str) -> bool: ...

    def needs_rehash(self, encoded: str) -> bool: ...

    def hash(self, password: str) -> str: ...


class LocalPasswordProvider:
    """精确读取 local 身份，并把 CPU 密集型 Argon2 校验移出事件循环。"""

    def __init__(
        self,
        repository: LocalAccountReader,
        hasher: PasswordVerifier,
    ) -> None:
        self.repository = repository
        self.hasher = hasher

    async def authenticate(
        self,
        login_name: str,
        password: str,
        *,
        pool: LocalHashPool = "auth_login_hash",
    ) -> AuthenticatedIdentity:
        normalized = normalize_login_name(login_name)
        record = await self.repository.find_local_account(normalized)
        password_hash = record.password_hash if record is not None else None
        try:
            password_matches = await run_bounded(
                self.hasher.verify_or_dummy,
                password_hash,
                password,
                timeout_s=5,
                pool=pool,
            )
        except (ExecutorBackpressure, TimeoutError):
            raise ProviderCapacityUnavailable("本地认证容量暂不可用") from None
        if (
            record is None or not password_matches or not record.account.active
            or not valid_account_department(record.account.role, record.account.dept)
        ):
            raise InvalidCredentials("用户名或密码错误")
        if record.account.must_change_password:
            current = await self.repository.find_local_account(normalized)
            if (
                current is None or not current.temporary_password_valid
                or not current.account.active or current.account != record.account
                or current.password_hash != record.password_hash
                or current.credential_version != record.credential_version
            ):
                raise InvalidCredentials("用户名或密码错误")
        await self._upgrade_password_hash(record, password)
        return AuthenticatedIdentity(
            provider_code="local",
            login_name=record.account.login_name,
            external_subject=f"local:{record.account.normalized_login_name}",
            display_name=record.account.display_name,
            dept=record.account.dept,
            groups=(),
            account=record.account,
        )

    async def _upgrade_password_hash(self, record: LocalAccountRecord, password: str) -> None:
        """成功校验后可选升级；线程只计算摘要，迟到结果没有数据库写入能力。"""

        try:
            if not self.hasher.needs_rehash(record.password_hash):
                return
            password_hash = await run_bounded(
                self.hasher.hash, password, timeout_s=5, pool="auth_hash",
            )
        except (ExecutorBackpressure, TimeoutError):
            # 参数维护不争用交互式登录池；容量不足时保留后续权威状态检查。
            return
        except Exception:
            raise ProviderUnavailable("本地凭据维护暂不可用") from None
        if not await self.repository.upgrade_local_password_hash(
            expected=record, password_hash=password_hash,
        ):
            raise InvalidCredentials("用户名或密码错误")
