"""本地密码认证 Provider。"""

from __future__ import annotations

from typing import Literal, Protocol

from app.core.auth.accounts import LocalAccountRecord
from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    ProviderCapacityUnavailable,
)
from app.core.auth.identity import normalize_login_name
from app.core.bounded_executor import ExecutorBackpressure, run_bounded

LocalHashPool = Literal["auth_login_hash", "auth_hash"]


class LocalAccountReader(Protocol):
    async def find_local_account(
        self,
        normalized_login_name: str,
    ) -> LocalAccountRecord | None: ...


class PasswordVerifier(Protocol):
    def verify_or_dummy(self, encoded: str | None, password: str) -> bool: ...


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
        if record is None or not password_matches or not record.account.active:
            raise InvalidCredentials("用户名或密码错误")
        return AuthenticatedIdentity(
            provider_code="local",
            login_name=record.account.login_name,
            external_subject=f"local:{record.account.normalized_login_name}",
            display_name=record.account.display_name,
            dept=record.account.dept,
            groups=(),
            account=record.account,
        )
