"""认证源的非敏感配置、版本状态机与公开展示契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

from app.core.auth.roles import Role

LDAP_ATTRIBUTE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
LDAP_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?$")
LDAP_CONFIG_FIELDS = frozenset(
    {
        "server",
        "base_dn",
        "bind_dn",
        "user_search_filter",
        "username_attribute",
        "display_name_attribute",
        "dept_attribute",
        "subject_attribute",
        "group_attribute",
        "connect_timeout_s",
        "receive_timeout_s",
    }
)


class ProviderError(RuntimeError):
    """认证源状态或配置错误。"""


class ProviderNotFound(ProviderError):
    """认证源不存在。"""


class ImmutableProvider(ProviderError):
    """内置本地认证源不可修改或禁用。"""


class InvalidProviderConfig(ProviderError):
    """认证源草稿不符合安全配置契约。"""


class UntestedProviderConfig(ProviderError):
    """当前草稿尚未成功测试，不能激活。"""


class StaleProviderDraft(ProviderError):
    """认证源测试期间草稿已被其他管理员修改。"""


class DuplicateRoleMapping(ProviderError):
    """同一认证源的外部组映射重复。"""


def validate_ldap_allowed(server: str, allowed_hosts: frozenset[str]) -> None:
    """部署侧精确允许列表；空列表或未命中目标时必须在使用 Bind Secret 前失败。"""

    parsed = urlsplit(server)
    hostname = (parsed.hostname or "").casefold()
    if not hostname or LDAP_HOST_RE.fullmatch(hostname) is None:
        raise InvalidProviderConfig("LDAP 地址无法解析为受控主机")
    if not allowed_hosts:
        raise InvalidProviderConfig("LDAP 出站目标未配置部署允许列表")
    port = parsed.port or 636
    matched = False
    for entry in allowed_hosts:
        entry_host, separator, raw_port = entry.partition(":")
        if not separator:
            if entry_host == hostname and port == 636:
                matched = True
                break
            continue
        try:
            entry_port = int(raw_port)
        except ValueError:
            raise InvalidProviderConfig("LDAP 允许列表端口无效") from None
        if entry_host == hostname and entry_port == port:
            matched = True
            break
    if not matched:
        raise InvalidProviderConfig("LDAP 目标不在部署允许列表")


@dataclass(frozen=True, slots=True)
class ProviderTestResult:
    """连接测试的无敏感结果。"""

    success: bool
    result_code: str


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """认证源完整服务层记录；仅管理员接口可读取非敏感配置。"""

    id: int
    code: str
    name: str
    kind: str
    enabled: bool
    draft_config: dict[str, object]
    active_config: dict[str, object] | None
    draft_version: int
    tested_version: int | None
    active_version: int | None
    last_tested_at: datetime | None
    last_test_status: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderSummary:
    """登录页可公开读取的最小认证源元数据。"""

    code: str
    name: str
    auth_flow: str


@dataclass(frozen=True, slots=True)
class ExternalRoleMapping:
    """外部目录组到平台角色和授权部门的显式映射。"""

    external_group: str
    role: Role
    dept: str | None = None


@dataclass(frozen=True, slots=True)
class LdapProviderConfig:
    """允许入库的 LDAP 非敏感配置白名单。"""

    server: str
    base_dn: str
    bind_dn: str
    user_search_filter: str
    username_attribute: str
    display_name_attribute: str
    dept_attribute: str
    subject_attribute: str
    group_attribute: str
    connect_timeout_s: float
    receive_timeout_s: float

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> LdapProviderConfig:
        """严格解析 LDAPS 草稿，拒绝密码和任何未声明字段。"""

        unknown = set(value) - LDAP_CONFIG_FIELDS
        missing = LDAP_CONFIG_FIELDS - set(value)
        if unknown:
            raise InvalidProviderConfig(f"包含未知配置项：{', '.join(sorted(unknown))}")
        if missing:
            raise InvalidProviderConfig(f"缺少配置项：{', '.join(sorted(missing))}")

        server = _required_text(value["server"], "LDAP 地址", max_length=512)
        parsed = urlsplit(server)
        if parsed.scheme.casefold() != "ldaps" or not parsed.hostname:
            raise InvalidProviderConfig("LDAP 地址必须使用有效的 LDAPS origin")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidProviderConfig("LDAP 地址不得包含凭据")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise InvalidProviderConfig("LDAP 地址必须是 LDAPS origin，不得包含路径或参数")

        base_dn = _required_text(value["base_dn"], "Base DN", max_length=512)
        bind_dn = _required_text(value["bind_dn"], "Bind DN", max_length=512)
        search_filter = _required_text(
            value["user_search_filter"],
            "用户查询过滤器",
            max_length=512,
        )
        without_username = search_filter.replace("{username}", "", 1)
        if (
            search_filter.count("{username}") != 1
            or "{" in without_username
            or "}" in without_username
        ):
            raise InvalidProviderConfig("用户查询过滤器必须且只能包含一个 {username} 占位符")

        attributes = {
            field: _required_text(value[field], "LDAP 属性", max_length=64)
            for field in (
                "username_attribute",
                "display_name_attribute",
                "dept_attribute",
                "subject_attribute",
                "group_attribute",
            )
        }
        if any(LDAP_ATTRIBUTE_RE.fullmatch(item) is None for item in attributes.values()):
            raise InvalidProviderConfig("LDAP 属性名只能包含字母、数字和短横线")

        connect_timeout = _timeout(value["connect_timeout_s"])
        receive_timeout = _timeout(value["receive_timeout_s"])
        return cls(
            server=server,
            base_dn=base_dn,
            bind_dn=bind_dn,
            user_search_filter=search_filter,
            username_attribute=attributes["username_attribute"],
            display_name_attribute=attributes["display_name_attribute"],
            dept_attribute=attributes["dept_attribute"],
            subject_attribute=attributes["subject_attribute"],
            group_attribute=attributes["group_attribute"],
            connect_timeout_s=connect_timeout,
            receive_timeout_s=receive_timeout,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "server": self.server,
            "base_dn": self.base_dn,
            "bind_dn": self.bind_dn,
            "user_search_filter": self.user_search_filter,
            "username_attribute": self.username_attribute,
            "display_name_attribute": self.display_name_attribute,
            "dept_attribute": self.dept_attribute,
            "subject_attribute": self.subject_attribute,
            "group_attribute": self.group_attribute,
            "connect_timeout_s": self.connect_timeout_s,
            "receive_timeout_s": self.receive_timeout_s,
        }


def _required_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise InvalidProviderConfig(f"{label}不能为空且不得超过 {max_length} 字符")
    return value.strip()


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidProviderConfig("LDAP 超时必须为 0–30 秒之间的数字")
    parsed = float(value)
    if parsed <= 0 or parsed > 30:
        raise InvalidProviderConfig("LDAP 超时必须大于 0 且不超过 30 秒")
    return parsed


class AuthProviderRepository(Protocol):
    async def list_enabled(self) -> tuple[ProviderRecord, ...]: ...

    async def get(self, code: str) -> ProviderRecord: ...

    async def save_draft(
        self,
        code: str,
        config: dict[str, object],
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord: ...

    async def record_test(
        self,
        code: str,
        version: int,
        result: ProviderTestResult,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord: ...

    async def activate(
        self,
        code: str,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord: ...

    async def disable(
        self,
        code: str,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord: ...

    async def list_role_mappings(
        self,
        code: str,
    ) -> tuple[ExternalRoleMapping, ...]: ...

    async def replace_role_mappings(
        self,
        code: str,
        mappings: tuple[ExternalRoleMapping, ...],
        *,
        actor: str,
        ip: str,
    ) -> tuple[ExternalRoleMapping, ...]: ...


class ProviderRegistry(Protocol):
    """按 kind 扩展配置校验、连接测试和认证流程元数据。"""

    def auth_flow(self, kind: str) -> str: ...

    def validate_config(
        self,
        kind: str,
        config: dict[str, object],
    ) -> dict[str, object]: ...

    async def test_config(
        self,
        kind: str,
        config: dict[str, object],
    ) -> ProviderTestResult: ...


class AuthProviderService:
    """认证源草稿、测试、激活与禁用的唯一状态机入口。"""

    def __init__(
        self,
        repository: AuthProviderRepository,
        registry: ProviderRegistry,
    ) -> None:
        self.repository = repository
        self.registry = registry

    async def list_enabled(self) -> tuple[ProviderSummary, ...]:
        records = await self.repository.list_enabled()
        return tuple(
            ProviderSummary(item.code, item.name, self.registry.auth_flow(item.kind))
            for item in records
        )

    async def get(self, code: str) -> ProviderRecord:
        return await self.repository.get(code)

    async def save_draft(
        self,
        code: str,
        config: dict[str, object],
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        record = await self.repository.get(code)
        self._ensure_mutable(record)
        validated = self._validate(record.kind, config)
        return await self.repository.save_draft(
            code,
            validated,
            actor=actor,
            ip=ip,
        )

    async def test_draft(
        self,
        code: str,
        *,
        actor: str,
        ip: str,
    ) -> ProviderTestResult:
        record = await self.repository.get(code)
        self._ensure_mutable(record)
        config = self._validate(record.kind, record.draft_config)
        result = await self.registry.test_config(record.kind, config)
        await self.repository.record_test(
            code,
            record.draft_version,
            result,
            actor=actor,
            ip=ip,
        )
        return result

    async def activate(self, code: str, *, actor: str, ip: str) -> ProviderRecord:
        record = await self.repository.get(code)
        self._ensure_mutable(record)
        return await self.repository.activate(code, actor=actor, ip=ip)

    async def disable(self, code: str, *, actor: str, ip: str) -> ProviderRecord:
        record = await self.repository.get(code)
        self._ensure_mutable(record)
        return await self.repository.disable(code, actor=actor, ip=ip)

    async def list_role_mappings(
        self,
        code: str,
    ) -> tuple[ExternalRoleMapping, ...]:
        record = await self.repository.get(code)
        self._ensure_mutable(record)
        return await self.repository.list_role_mappings(code)

    async def replace_role_mappings(
        self,
        code: str,
        mappings: tuple[ExternalRoleMapping, ...],
        *,
        actor: str,
        ip: str,
    ) -> tuple[ExternalRoleMapping, ...]:
        record = await self.repository.get(code)
        self._ensure_mutable(record)
        normalized: list[ExternalRoleMapping] = []
        seen: set[str] = set()
        for mapping in mappings:
            group = mapping.external_group.strip()
            if not group or len(group) > 256:
                raise InvalidProviderConfig("外部组名称不能为空且不得超过 256 字符")
            dept = (mapping.dept or "").strip()
            if not dept or len(dept) > 128:
                raise InvalidProviderConfig("外部组的授权部门不能为空且不得超过 128 字符")
            key = group.casefold()
            if key in seen:
                raise DuplicateRoleMapping("同一外部组只能映射一次")
            seen.add(key)
            normalized.append(ExternalRoleMapping(group, mapping.role, dept))
        return await self.repository.replace_role_mappings(
            code,
            tuple(normalized),
            actor=actor,
            ip=ip,
        )

    def _validate(self, kind: str, config: dict[str, object]) -> dict[str, object]:
        normalized = (
            LdapProviderConfig.from_mapping(config).to_mapping() if kind == "ldap" else dict(config)
        )
        return self.registry.validate_config(kind, normalized)

    @staticmethod
    def _ensure_mutable(record: ProviderRecord) -> None:
        if record.code == "local":
            raise ImmutableProvider("内置本地认证源始终启用且不可修改")
