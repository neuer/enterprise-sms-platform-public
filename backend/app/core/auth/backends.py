"""密码型认证 Provider 共用的结果模型与稳定异常。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.core.auth.accounts import PlatformAccount


class InvalidCredentials(RuntimeError):
    """统一的用户名或密码错误，不暴露账号是否存在。"""


class ProviderDisabled(RuntimeError):
    """认证源不存在、未启用或缺少生效配置。"""


class ProviderUnavailable(RuntimeError):
    """认证源基础设施或实现暂不可用。"""


class ProviderCapacityUnavailable(ProviderUnavailable):
    """认证源同步容量已满或超过完整底层 I/O 时限。"""


class SessionStateUnavailable(RuntimeError):
    """数据库或 Redis 会话权威状态不可用；所有鉴权路径必须 fail closed。"""


# 旧名称只用于重构期间尚未迁移的调用方，不改变新 API 错误语义。
DirectoryUnavailable = ProviderUnavailable

DevelopmentRole = Literal["admin", "approver", "operator", "viewer"]


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    """所有密码型 Provider 返回的统一身份结果。"""

    provider_code: str
    login_name: str
    external_subject: str
    display_name: str
    dept: str
    groups: tuple[str, ...]
    development_role: DevelopmentRole | None = None
    account: PlatformAccount | None = None


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """待迁移用户管理代码的临时兼容投影。"""

    username: str
    display_name: str
    dept: str
    groups: tuple[str, ...]


class PasswordAuthProvider(Protocol):
    async def authenticate(
        self,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity: ...
