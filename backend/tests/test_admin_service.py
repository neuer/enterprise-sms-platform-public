from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.admin import (
    AdminService,
    AuditQuery,
    AuditRecord,
    ConfigRow,
    ConfigUpdate,
    InvalidAdminQuery,
)

ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")


class FakeRepository:
    def __init__(self) -> None:
        self.updates: list[tuple[tuple[ConfigUpdate, ...], SecurityPrincipal, str]] = []
        self.audit_queries: list[AuditQuery] = []

    async def list_configs(self) -> tuple[ConfigRow, ...]:
        return (
            ConfigRow("vendor_qps", "5", "int", "厂商 QPS", None, None),
            ConfigRow("reserved_realtime_qps", "2", "int", "实时预留", None, None),
            ConfigRow("report_poll_seconds", "60", "int", "报告轮询", None, None),
            ConfigRow("unsubscribe_auto_append", "true", "bool", "自动退订语", None, None),
            ConfigRow(
                "alert_wecom_webhook",
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=current",
                "str",
                "Webhook",
                None,
                None,
            ),
            ConfigRow("alert_mail_to", "", "str", "收件人", None, None),
            ConfigRow("alert_smtp_host", "smtp", "str", "SMTP 主机", None, None),
            ConfigRow("market_send_window", "08:00-21:00", "str", "营销窗口", None, None),
            ConfigRow("callback_allow_cidrs", "10.0.0.0/8", "str", "回调网段", None, None),
            ConfigRow("alert_smtp_port", "25", "int", "SMTP 端口", None, None),
            ConfigRow("fail_rate_threshold", "20", "int", "失败率", None, None),
            ConfigRow(
                "callback_retry_schedule",
                "60,300,900,3600,3600",
                "str",
                "回调重试",
                None,
                None,
            ),
        )

    async def update_configs(
        self,
        updates: tuple[ConfigUpdate, ...],
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[ConfigRow, ...]:
        self.updates.append((updates, principal, ip))
        return await self.list_configs()

    async def list_audits(self, query: AuditQuery) -> tuple[tuple[AuditRecord, ...], int]:
        self.audit_queries.append(query)
        return ((), 0)


@pytest.mark.asyncio
async def test_configs_are_grouped_and_sensitive_value_is_never_returned() -> None:
    service = AdminService(FakeRepository())

    items = await service.list_configs()

    webhook = next(item for item in items if item.key == "alert_wecom_webhook")
    assert webhook.value is None and webhook.configured is True and webhook.sensitive is True
    report = next(item for item in items if item.key == "report_poll_seconds")
    assert report.group == "运行调度" and report.beat_restart_required is True


@pytest.mark.asyncio
async def test_config_update_validates_types_duplicates_and_sensitive_keep_semantics() -> None:
    repository = FakeRepository()
    service = AdminService(repository)

    await service.update_configs(
        (
            ConfigUpdate("vendor_qps", "8"),
            ConfigUpdate("unsubscribe_auto_append", "false"),
            ConfigUpdate("alert_wecom_webhook", None),
        ),
        principal=ADMIN,
        ip="10.0.0.8",
    )

    assert repository.updates[0][0] == (
        ConfigUpdate("vendor_qps", "8"),
        ConfigUpdate("unsubscribe_auto_append", "false"),
    )
    with pytest.raises(InvalidAdminQuery, match="重复"):
        await service.update_configs(
            (ConfigUpdate("vendor_qps", "5"), ConfigUpdate("vendor_qps", "6")),
            principal=ADMIN,
            ip="10.0.0.8",
        )
    with pytest.raises(InvalidAdminQuery, match="正整数"):
        await service.update_configs(
            (ConfigUpdate("vendor_qps", "0"),), principal=ADMIN, ip="10.0.0.8"
        )
    with pytest.raises(InvalidAdminQuery, match="布尔"):
        await service.update_configs(
            (ConfigUpdate("unsubscribe_auto_append", "yes"),),
            principal=ADMIN,
            ip="10.0.0.8",
        )


@pytest.mark.asyncio
async def test_audit_query_requires_aware_ordered_range() -> None:
    repository = FakeRepository()
    service = AdminService(repository)
    start = datetime(2026, 7, 12, 8, tzinfo=UTC)
    end = datetime(2026, 7, 12, 9, tzinfo=UTC)

    await service.list_audits(AuditQuery(None, None, None, start, end, 1, 20))

    assert repository.audit_queries[0].start == start
    with pytest.raises(InvalidAdminQuery, match="时区"):
        await service.list_audits(AuditQuery(None, None, None, datetime(2026, 7, 12), None, 1, 20))
    with pytest.raises(InvalidAdminQuery, match="晚于"):
        await service.list_audits(AuditQuery(None, None, None, end, start, 1, 20))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("market_send_window", "21:00-08:00"),
        ("callback_allow_cidrs", "not-a-network"),
        ("callback_allow_cidrs", "0.0.0.0/0"),
        ("callback_allow_cidrs", "100.64.0.0/10"),
        ("callback_allow_cidrs", "127.0.0.0/8"),
        ("callback_allow_cidrs", "169.254.0.0/16"),
        ("alert_smtp_port", "70000"),
        ("fail_rate_threshold", "101"),
        ("callback_retry_schedule", "60,300"),
    ),
)
async def test_invalid_semantic_config_is_rejected_before_any_write(
    key: str,
    value: str,
) -> None:
    repository = FakeRepository()

    with pytest.raises(InvalidAdminQuery):
        await AdminService(repository).update_configs(
            (ConfigUpdate(key, value),), principal=ADMIN, ip="10.0.0.8"
        )

    assert repository.updates == []


@pytest.mark.asyncio
async def test_cross_field_config_is_validated_atomically() -> None:
    repository = FakeRepository()

    with pytest.raises(InvalidAdminQuery, match="reserved_realtime_qps"):
        await AdminService(repository).update_configs(
            (
                ConfigUpdate("vendor_qps", "8"),
                ConfigUpdate("reserved_realtime_qps", "8"),
            ),
            principal=ADMIN,
            ip="10.0.0.8",
        )

    assert repository.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("alert_wecom_webhook", "https://attacker.example/hook"),
        ("alert_wecom_webhook", "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"),
        ("alert_smtp_host", "attacker.internal"),
    ],
)
async def test_alert_egress_config_is_rejected_before_write(key: str, value: str) -> None:
    repository = FakeRepository()

    with pytest.raises(InvalidAdminQuery):
        await AdminService(
            repository,
            allowed_smtp_hosts={"smtp", "mail.internal"},
        ).update_configs(
            (ConfigUpdate(key, value),),
            principal=ADMIN,
            ip="10.0.0.8",
        )

    assert repository.updates == []
