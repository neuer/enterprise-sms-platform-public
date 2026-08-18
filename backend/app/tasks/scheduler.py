"""Celery beat 启动时一次性读取数据库调度间隔。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text

from app.core.jobtrack import JOB_SPECS, JobSpec
from app.core.runtime_resources import (
    close_runtime_resources,
    configure_runtime_resources,
    database_engine,
)
from app.settings import get_settings

STARTUP_SCHEDULE_ENV = "SMS_BEAT_SCHEDULE_JSON"
CONFIGURABLE_JOB_SCHEDULES = {
    "poll-report": "poll_report",
    "poll-reply": "poll_reply",
    "reconcile": "reconcile",
    "expire-approvals": "expire_approvals",
    "dispatch-scheduled": "dispatch_scheduled",
    "poll-balance": "poll_balance",
    "anomaly-scan": "anomaly_scan",
    "usage-projection-reconcile": "reconcile_usage_projection",
}


def build_beat_schedule(config: dict[str, str]) -> dict[str, dict[str, Any]]:
    """构造固定到本次 beat 生命周期的调度表，不提供运行时热更。"""

    report_seconds = int(config.get("report_poll_seconds", "60"))
    reply_seconds = int(config.get("reply_poll_seconds", "300"))
    reconcile_seconds = int(config.get("reconcile_interval_min", "5")) * 60
    approval_seconds = int(config.get("approval_scan_seconds", "300"))
    scheduled_seconds = int(config.get("scheduled_scan_seconds", "60"))
    balance_seconds = int(config.get("balance_poll_seconds", "600"))
    anomaly_seconds = int(config.get("anomaly_scan_minutes", "60")) * 60
    usage_projection_seconds = int(config.get("usage_projection_reconcile_seconds", "300"))
    if (
        min(
            report_seconds,
            reply_seconds,
            reconcile_seconds,
            approval_seconds,
            scheduled_seconds,
            balance_seconds,
            anomaly_seconds,
            usage_projection_seconds,
        )
        < 1
    ):
        raise ValueError("beat intervals must be positive")
    return {
        "poll-report": {
            "task": "app.tasks.poll_report",
            "schedule": report_seconds,
            "options": {"queue": "realtime"},
        },
        "poll-reply": {
            "task": "app.tasks.poll_reply",
            "schedule": reply_seconds,
            "options": {"queue": "realtime"},
        },
        "reconcile": {
            "task": "app.tasks.reconcile",
            "schedule": reconcile_seconds,
            "options": {"queue": "realtime"},
        },
        "expire-approvals": {
            "task": "app.tasks.expire_approvals",
            "schedule": approval_seconds,
            "options": {"queue": "realtime"},
        },
        "dispatch-scheduled": {
            "task": "app.tasks.dispatch_scheduled",
            "schedule": scheduled_seconds,
            "options": {"queue": "realtime"},
        },
        "sync-templates": {
            "task": "app.tasks.sync_templates",
            "schedule": 600,
            "options": {"queue": "realtime"},
        },
        "sync-signs": {
            "task": "app.tasks.sync_signs",
            "schedule": 600,
            "options": {"queue": "realtime"},
        },
        "poll-balance": {
            "task": "app.tasks.poll_balance",
            "schedule": balance_seconds,
            "options": {"queue": "realtime"},
        },
        "anomaly-scan": {
            "task": "app.tasks.anomaly_scan",
            "schedule": anomaly_seconds,
            "options": {"queue": "realtime"},
        },
        "dispatch-callbacks": {
            "task": "app.tasks.dispatch_callbacks",
            "schedule": 30,
            "options": {"queue": "callback"},
        },
        "dispatch-exports": {
            "task": "app.tasks.dispatch_exports",
            "schedule": 60,
            "options": {"queue": "bulk"},
        },
        "dispatch-imports": {
            "task": "app.tasks.dispatch_imports",
            "schedule": 30,
            "options": {"queue": "bulk"},
        },
        "cleanup-exports": {
            "task": "app.tasks.cleanup_exports",
            "schedule": 3600,
            "options": {"queue": "bulk"},
        },
        "aggregate-stats": {
            "task": "app.tasks.aggregate_stats",
            "schedule": 300,
            "options": {"queue": "bulk"},
        },
        "housekeeping": {
            "task": "app.tasks.housekeeping",
            "schedule": 86400,
            "options": {"queue": "bulk"},
        },
        "usage-projection-reconcile": {
            "task": "app.tasks.reconcile_usage_projection",
            "schedule": usage_projection_seconds,
            "options": {"queue": "realtime"},
        },
        "security-daily-generate": {
            "task": "app.tasks.security_daily_generate",
            "schedule": 60,
            "options": {"queue": "bulk"},
        },
    }


def apply_job_interval_overrides(schedule: dict[str, dict[str, Any]]) -> None:
    """用进程启动快照校准可配置任务的心跳预期间隔。"""

    for schedule_name, job_name in CONFIGURABLE_JOB_SCHEDULES.items():
        item = schedule.get(schedule_name)
        if item is None or job_name not in JOB_SPECS:
            raise ValueError(f"missing startup job schedule: {schedule_name}")
        interval = item.get("schedule")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
            raise ValueError(f"invalid startup job interval: {schedule_name}")
        JOB_SPECS[job_name] = JobSpec(job_name, interval)


async def load_and_apply_job_intervals() -> None:
    """API 启动时读取一次；运行期间不热更。"""

    apply_job_interval_overrides(await load_beat_schedule())


async def load_beat_schedule() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    engine = database_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT key,value FROM sys_config "
                    "WHERE key IN ("
                    "'report_poll_seconds','reply_poll_seconds','reconcile_interval_min',"
                    "'approval_scan_seconds',"
                    "'scheduled_scan_seconds','balance_poll_seconds'"
                    ",'anomaly_scan_minutes'"
                    ",'usage_projection_reconcile_seconds'"
                    ")"
                )
            )
            config = {str(row["key"]): str(row["value"]) for row in result.mappings()}
            return build_beat_schedule(config)
    finally:
        await engine.dispose()


def encode_startup_schedule(schedule: dict[str, dict[str, Any]]) -> str:
    """把不含凭据的启动调度编码给 Celery beat 子进程。"""

    return json.dumps(schedule, sort_keys=True, separators=(",", ":"))


def decode_startup_schedule(raw: str | None) -> dict[str, dict[str, Any]]:
    """在 Celery scheduler 构造前恢复本次启动固定的调度。"""

    if raw is None:
        return {}
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("invalid beat startup schedule") from error
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not isinstance(item, dict) for name, item in value.items()
    ):
        raise ValueError("invalid beat startup schedule")
    return value


def load_startup_schedule() -> dict[str, dict[str, Any]]:
    """同步入口仅供 beat 父进程在创建 Celery 子进程前调用。"""

    async def load_and_close() -> dict[str, dict[str, Any]]:
        configure_runtime_resources(get_settings(), component="beat")
        try:
            return await load_beat_schedule()
        finally:
            await close_runtime_resources()

    return asyncio.run(load_and_close())
