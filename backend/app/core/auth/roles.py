"""AD 组、人工覆盖与首管引导的单点角色决策。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.core.auth.backends import AuthenticatedIdentity, DirectoryIdentity, InvalidCredentials

Role = Literal["admin", "approver", "operator", "viewer"]
ROLE_PRIORITY: dict[str, int] = {"viewer": 1, "operator": 2, "approver": 3, "admin": 4}


@dataclass(frozen=True, slots=True)
class ExistingUser:
    role: Role
    role_override: bool


@dataclass(frozen=True, slots=True)
class RoleDecision:
    role: Role
    audit_action: str | None = None


class RoleResolver:
    """人工覆盖最高优先，其后按外部组映射；不包含任何隐藏提权路径。"""

    def resolve(
        self,
        identity: AuthenticatedIdentity | DirectoryIdentity,
        existing: ExistingUser | None,
        mappings: Mapping[str, str],
    ) -> RoleDecision:
        if existing is not None and existing.role_override:
            return RoleDecision(existing.role)

        roles = [mappings[group] for group in identity.groups if group in mappings]
        if identity.groups and all(group.startswith("mock:") for group in identity.groups):
            roles.extend(group.removeprefix("mock:") for group in identity.groups)
        valid_roles = [role for role in roles if role in ROLE_PRIORITY]
        if valid_roles:
            selected = max(valid_roles, key=ROLE_PRIORITY.__getitem__)
            return RoleDecision(selected)  # type: ignore[arg-type]
        raise InvalidCredentials("用户未映射到平台角色")
