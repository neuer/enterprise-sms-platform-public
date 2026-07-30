"""管理员审计查询与系统参数安全编排。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.core.auth.accounts import SecurityPrincipal
from app.services.alert import validate_alert_destinations
from app.services.runtime_policy import (
    BEAT_STARTUP_ONLY_KEYS,
    InvalidRuntimePolicy,
    RuntimePolicy,
)


class InvalidAdminQuery(ValueError):
    """管理员查询或配置值不符合安全约束。"""


@dataclass(frozen=True, slots=True)
class AuditQuery:
    actor: str | None
    action: str | None
    object_type: str | None
    start: datetime | None
    end: datetime | None
    page: int
    page_size: int
    actor_account_id: int | None = None
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: int
    actor: str
    role: str | None
    ip: str | None
    action: str
    object_type: str | None
    object_id: str | None
    before_val: dict[str, Any] | None
    after_val: dict[str, Any] | None
    created_at: datetime
    actor_subject_kind: str = "legacy_unknown"
    actor_account_id: int | None = None
    actor_identity_id: int | None = None
    actor_app_id: int | None = None
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConfigRow:
    key: str
    value: str
    value_type: str
    description: str | None
    updated_by: str | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConfigUpdate:
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class ConfigItem:
    key: str
    value: str | None
    value_type: str
    description: str | None
    group: str
    sensitive: bool
    configured: bool
    beat_restart_required: bool
    updated_by: str | None
    updated_at: datetime | None


class AdminRepository(Protocol):
    async def list_configs(self) -> tuple[ConfigRow, ...]: ...

    async def update_configs(
        self,
        updates: tuple[ConfigUpdate, ...],
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[ConfigRow, ...]: ...

    async def list_audits(
        self,
        query: AuditQuery,
    ) -> tuple[tuple[AuditRecord, ...], int]: ...


SENSITIVE_CONFIG_KEYS = frozenset({"alert_wecom_webhook"})
BEAT_CONFIG_KEYS = BEAT_STARTUP_ONLY_KEYS


def _group(key: str) -> str:
    if key in BEAT_CONFIG_KEYS or key.startswith(("vendor_", "reserved_realtime")):
        return "运行调度"
    if key.startswith(("alert_", "fail_rate", "balance_alert", "uncertain_alert")):
        return "告警通知"
    if key.endswith(("retention_days", "retention_months", "expire_hours")) or key in {
        "job_history_days",
        "msg_retention_months",
        "audit_retention_months",
    }:
        return "生命周期"
    if key.startswith(("login_", "callback_", "key_grace")):
        return "安全控制"
    return "发送策略"


class AdminService:
    """对查询时间、配置类型和敏感配置回显实行 fail-closed。"""

    def __init__(
        self,
        repository: AdminRepository,
        *,
        allowed_smtp_hosts: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.repository = repository
        if allowed_smtp_hosts is None:
            from app.settings import get_settings

            allowed_smtp_hosts = get_settings().alert_smtp_allowed_host_set
        self.allowed_smtp_hosts = frozenset(allowed_smtp_hosts)

    @staticmethod
    def _item(row: ConfigRow) -> ConfigItem:
        sensitive = row.key in SENSITIVE_CONFIG_KEYS
        return ConfigItem(
            key=row.key,
            value=None if sensitive else row.value,
            value_type=row.value_type,
            description=row.description,
            group=_group(row.key),
            sensitive=sensitive,
            configured=bool(row.value),
            beat_restart_required=row.key in BEAT_CONFIG_KEYS,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )

    async def list_configs(self) -> tuple[ConfigItem, ...]:
        return tuple(self._item(row) for row in await self.repository.list_configs())

    @staticmethod
    def _validate_value(row: ConfigRow, value: str) -> str:
        if len(value) > 512:
            raise InvalidAdminQuery(f"配置 {row.key} 超过最大长度")
        if row.value_type == "int":
            try:
                number = int(value)
            except ValueError as error:
                raise InvalidAdminQuery(f"配置 {row.key} 必须为正整数") from error
            minimum = 0 if row.key == "reserved_realtime_qps" else 1
            if str(number) != value.strip() or number < minimum:
                raise InvalidAdminQuery(f"配置 {row.key} 必须为正整数")
            return str(number)
        if row.value_type == "bool":
            normalized = value.strip().lower()
            if normalized not in {"true", "false"}:
                raise InvalidAdminQuery(f"配置 {row.key} 必须为布尔值 true/false")
            return normalized
        if row.value_type != "str":
            raise InvalidAdminQuery(f"配置 {row.key} 类型不受支持")
        if row.key == "sensitive_hit_action" and value not in {"block", "audit"}:
            raise InvalidAdminQuery("敏感词策略只允许 block/audit")
        return value

    async def update_configs(
        self,
        updates: tuple[ConfigUpdate, ...],
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[ConfigItem, ...]:
        if not updates:
            raise InvalidAdminQuery("配置更新不能为空")
        keys = [item.key for item in updates]
        if len(keys) != len(set(keys)):
            raise InvalidAdminQuery("配置 key 重复")
        rows = await self.repository.list_configs()
        known = {row.key: row for row in rows}
        normalized: list[ConfigUpdate] = []
        for item in updates:
            row = known.get(item.key)
            if row is None:
                raise InvalidAdminQuery(f"未知配置: {item.key}")
            if item.value is None:
                if item.key not in SENSITIVE_CONFIG_KEYS:
                    raise InvalidAdminQuery(f"配置 {item.key} 缺少值")
                continue
            normalized.append(ConfigUpdate(item.key, self._validate_value(row, item.value)))
        if not normalized:
            return tuple(self._item(row) for row in rows)
        effective = {row.key: row.value for row in rows}
        effective.update({item.key: item.value or "" for item in normalized})
        try:
            RuntimePolicy.from_mapping(effective)
            validate_alert_destinations(effective, self.allowed_smtp_hosts)
        except InvalidRuntimePolicy as error:
            raise InvalidAdminQuery(str(error)) from error
        except ValueError as error:
            raise InvalidAdminQuery(str(error)) from error
        changed = await self.repository.update_configs(
            tuple(normalized),
            principal=principal,
            ip=ip,
        )
        return tuple(self._item(row) for row in changed)

    async def list_audits(
        self,
        query: AuditQuery,
    ) -> tuple[tuple[AuditRecord, ...], int]:
        for moment in (query.start, query.end):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise InvalidAdminQuery("审计时间必须包含时区")
        if query.start is not None and query.end is not None and query.start > query.end:
            raise InvalidAdminQuery("审计开始时间不得晚于结束时间")
        if query.actor_account_id is not None and query.actor_account_id < 1:
            raise InvalidAdminQuery("actor_account_id 必须为正整数")
        for label, value, limit in (
            ("actor", query.actor, 64),
            ("action", query.action, 48),
            ("object_type", query.object_type, 32),
        ):
            if value is not None and len(value) > limit:
                raise InvalidAdminQuery(f"{label} 过滤值过长")
        return await self.repository.list_audits(query)
