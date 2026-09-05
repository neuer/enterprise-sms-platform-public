"""平台主体、认证身份与本地凭据投影。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.auth.roles import Role


class AccountSourceConflict(RuntimeError):
    """规范化登录名已由其他认证身份先占用。"""


class AccountNotFound(LookupError):
    """稳定主体 ID 不存在。"""


@dataclass(frozen=True, slots=True)
class SecurityPrincipal:
    """授权、所有权与审计使用的不可变主体，以及事件时展示快照。"""

    account_id: int
    identity_id: int
    login_name: str
    dept: str
    role: Role

    def __post_init__(self) -> None:
        if self.account_id < 1 or self.identity_id < 1:
            raise ValueError("稳定主体 ID 无效")
        if not self.login_name or len(self.login_name) > 64:
            raise ValueError("主体展示登录名无效")
        if len(self.dept) > 128:
            raise ValueError("主体部门快照无效")

    @property
    def actor_name(self) -> str:
        """返回仅供展示的事件时登录名快照。"""

        return self.login_name

    @property
    def subject_kind(self) -> Literal["human"]:
        return "human"

    @property
    def actor_account_id(self) -> int:
        return self.account_id

    @property
    def actor_identity_id(self) -> int:
        return self.identity_id

    @property
    def actor_app_id(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ApplicationPrincipal:
    """API Key 调用方的稳定应用主体与事件时名称/部门快照。"""

    app_id: int
    name: str
    dept: str

    def __post_init__(self) -> None:
        if self.app_id < 1:
            raise ValueError("稳定应用主体 ID 无效")
        if not self.name or len(self.name) > 64:
            raise ValueError("应用主体名称无效")
        if not self.dept or len(self.dept) > 128:
            raise ValueError("应用主体部门无效")

    @property
    def actor_name(self) -> str:
        """延续安全、无密钥信息的应用主体展示格式。"""

        return f"app:{self.app_id}"

    @property
    def subject_kind(self) -> Literal["api_app"]:
        return "api_app"

    @property
    def actor_account_id(self) -> None:
        return None

    @property
    def actor_identity_id(self) -> None:
        return None

    @property
    def actor_app_id(self) -> int:
        return self.app_id


@dataclass(frozen=True, slots=True)
class UncertainEffectPrincipal:
    """不可伪造的 uncertain 人工重发执行主体；只能由已批准 resolution 构造。"""

    resolution_id: int
    proposer_account_id: int
    confirmer_account_id: int
    effect_generation: int
    dept: str

    def __post_init__(self) -> None:
        if (
            self.resolution_id < 1
            or self.proposer_account_id < 1
            or self.confirmer_account_id < 1
            or self.effect_generation < 1
        ):
            raise ValueError("uncertain effect principal invalid")
        if self.proposer_account_id == self.confirmer_account_id:
            raise ValueError("uncertain effect principal must be dual-controlled")
        if not self.dept or len(self.dept) > 128:
            raise ValueError("uncertain effect department invalid")

    @property
    def actor_name(self) -> str:
        return f"system_resend:{self.resolution_id}"

    @property
    def subject_kind(self) -> Literal["system"]:
        return "system"

    @property
    def actor_account_id(self) -> int:
        return self.confirmer_account_id

    @property
    def actor_identity_id(self) -> None:
        return None

    @property
    def actor_app_id(self) -> None:
        return None

    @property
    def account_id(self) -> int:
        return self.confirmer_account_id


type ActorPrincipal = SecurityPrincipal | ApplicationPrincipal | UncertainEffectPrincipal


@dataclass(frozen=True, slots=True)
class PlatformAccount:
    """认证后可用于签发令牌的平台主体与身份快照。"""

    account_id: int
    identity_id: int
    provider_code: str
    login_name: str
    normalized_login_name: str
    display_name: str
    dept: str
    role: Role
    security_version: int
    account_enabled: bool
    identity_enabled: bool
    provider_enabled: bool = True
    must_change_password: bool = False

    @property
    def active(self) -> bool:
        return self.account_enabled and self.identity_enabled and self.provider_enabled


@dataclass(frozen=True, slots=True)
class LocalAccountRecord:
    """只在本地认证边界内部使用的账号与 Argon2id 哈希投影。"""

    account: PlatformAccount
    password_hash: str
    credential_version: int = 1
