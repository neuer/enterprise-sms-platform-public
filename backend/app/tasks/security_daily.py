"""在 08:00 后消费脱敏安全证据快照并建立日报事实。"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.security_daily import (
    FileSecurityDailyControl,
    SecurityDailyService,
    generate_security_daily_for_date,
)
from app.services.security_daily_repository import SqlSecurityDailyRepository
from app.settings import get_settings
from app.tasks import background_task_options, celery_app

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
async def generate_security_daily_once(
    repository: SqlSecurityDailyRepository,
    control_dir: Path,
    *,
    now: datetime,
    enabled: bool,
    recipient_count: int,
) -> int:
    """每分钟检查一次，08:00 后只处理前一上海自然日，缺证据则显式 unavailable。"""

    local_now = now.astimezone(SHANGHAI_TZ)
    if not enabled or local_now.time() < time(8, 0):
        return 0
    report_date = local_now.date() - timedelta(days=1)
    return int(
        await generate_security_daily_for_date(
            repository,
            control_dir,
            report_date=report_date,
            recipient_count=recipient_count,
        )
    )


async def _generate() -> int:
    settings = get_settings()
    repository = SqlSecurityDailyRepository(settings)
    enabled, recipient_count = await repository.generation_config()
    now = datetime.now(SHANGHAI_TZ)
    changed = await generate_security_daily_once(
        repository,
        settings.security_daily_control_dir,
        now=now,
        enabled=enabled,
        recipient_count=recipient_count,
    )
    if enabled and now.time() >= time(8, 0):
        # 自动投递：正常报告与问题通报都提交；幂等保证每天最多一次。
        service = SecurityDailyService(
            repository,
            FileSecurityDailyControl(
                settings.security_daily_control_dir,
                settings.security_daily_config_dir,
            ),
            control_dir=settings.security_daily_control_dir,
        )
        report_date = now.astimezone(SHANGHAI_TZ).date() - timedelta(days=1)
        await service.submit_auto_delivery(report_date)
    return changed


@celery_app.task(
    name="app.tasks.security_daily_generate",
    **background_task_options(soft_time_limit=120, time_limit=150),
)  # type: ignore[untyped-decorator]
@tracked_job("security_daily_generate", expect_interval_s=60)
def security_daily_generate() -> int:
    """任务参数只含时间和控制目录路径，不携带报告正文或原始日志。"""

    return run_worker_async(_generate())


__all__ = ["generate_security_daily_once", "security_daily_generate"]
