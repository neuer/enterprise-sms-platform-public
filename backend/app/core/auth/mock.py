"""AUTH_MOCK=1 时只替换 AD Provider 的 seed-dev 实现。"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials


@dataclass(frozen=True, slots=True)
class _MockUser:
    display_name: str
    dept: str
    role: str


MOCK_USERS = {
    "admin01": _MockUser("开发管理员", "平台技术部", "admin"),
    "approver01": _MockUser("开发审批员", "业务一部", "approver"),
    "operator01": _MockUser("开发操作员", "业务一部", "operator"),
    "viewer01": _MockUser("开发查看员", "业务一部", "viewer"),
}


class MockLdapProvider:
    """只接受 seed-dev 四用户与运行时注入的开发密码，认证源固定为 ad。"""

    def __init__(self, expected_password: str) -> None:
        if not expected_password:
            raise ValueError("mock authentication password must not be empty")
        self.expected_password = expected_password

    async def authenticate(
        self,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        user = MOCK_USERS.get(login_name)
        password_matches = hmac.compare_digest(password, self.expected_password)
        if user is None or not password_matches:
            raise InvalidCredentials("用户名或密码错误")
        return AuthenticatedIdentity(
            provider_code="ad",
            login_name=login_name,
            external_subject=f"mock:{login_name}",
            display_name=user.display_name,
            dept=user.dept,
            groups=(f"mock:{user.role}",),
        )


# 兼容尚未迁移的导入；运行装配只使用 MockLdapProvider。
MockAuthBackend = MockLdapProvider
