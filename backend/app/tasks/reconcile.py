"""uncertain 证据对账任务；后续 M2 在此扩展队列兜底重投。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.core.jobtrack import tracked_job
from app.core.runtime_resources import redis_client
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.crypto import CryptoService
from app.services.ops_repository import SqlOpsRepository
from app.services.queue import CeleryQueuePublisher
from app.services.raw_replay import RawReplayConflict, RawReplayNotFound, RawReplayService
from app.services.reconcile import RecoveryReconciler
from app.services.reconcile_repository import SqlRecoveryRepository
from app.services.reply_ingest import ReplyIngestService
from app.services.reply_repository import SqlReplyRepository
from app.services.report_ingest import ReportIngestService
from app.services.report_repository import SqlReportRepository
from app.services.runtime_policy import SqlRuntimePolicyLoader
from app.services.uncertain import UncertainReconciler
from app.services.uncertain_repository import SqlUncertainRepository
from app.services.vendor_control_client import VendorControlClient
from app.services.vendor_test_operation import VendorTestOperationService
from app.services.vendor_test_operation_repository import (
    SqlVendorTestOperationRepository,
)
from app.services.vendor_test_recipient_repository import (
    SqlVendorTestRecipientRepository,
)
from app.services.vendor_test_reset import VendorTestResetFinalizer
from app.services.vendor_test_uat import VendorTestUatReconciler
from app.settings import Settings, get_settings
from app.tasks import celery_app

LOGGER = logging.getLogger(__name__)


class ReconcilePartialFailure(RuntimeError):
    """一轮对账中至少一个恢复域失败；其余域已经继续执行。"""


async def _run_domain(
    name: str,
    operation: Callable[[], Awaitable[int]],
    settings: Settings,
    failures: list[BaseException],
) -> int:
    """单个恢复域失败时告警并继续，不得静默吞错。"""

    try:
        return await operation()
    except Exception as error:
        LOGGER.error(
            "reconcile domain failed",
            extra={"domain": name, "error_type": type(error).__name__},
        )
        try:
            await SqlAlertService(settings).emit(
                alert_type="reconcile_domain_failed",
                level="crit",
                title=f"恢复域 {name} 失败，本轮其余域已继续",
                detail={"domain": name, "error_type": type(error).__name__},
                dedup_key=f"reconcile_domain_failed:{name}",
            )
        except Exception as alert_error:
            LOGGER.error(
                "reconcile domain alert unavailable",
                extra={"domain": name, "error_type": type(alert_error).__name__},
            )
        failures.append(error)
        return 0


async def _reconcile() -> int:
    settings = get_settings()
    policy = await SqlRuntimePolicyLoader(settings).load()
    operation_repository = SqlVendorTestOperationRepository(settings)
    failures: list[BaseException] = []

    async def uncertain() -> int:
        return await UncertainReconciler.from_policy(
            SqlUncertainRepository(settings),
            CryptoService.from_settings(settings),
            policy,
        ).run_once()

    async def recovery() -> int:
        return await RecoveryReconciler(
            SqlRecoveryRepository(settings),
            CeleryQueuePublisher(),
        ).run_once()

    async def vendor_control() -> int:
        return await VendorTestOperationService(
            operation_repository,
            VendorControlClient(),
            finalizers={
                "reset_configuration": VendorTestResetFinalizer(
                    SqlVendorTestRecipientRepository(settings)
                ),
            },
        ).reconcile_once()

    async def vendor_uat() -> int:
        return await VendorTestUatReconciler(operation_repository).reconcile_once()

    total = 0
    for name, operation in (
        ("uncertain", uncertain),
        ("delivery-recovery", recovery),
        ("vendor-control", vendor_control),
        ("vendor-uat", vendor_uat),
        ("raw-replay", lambda: _replay_stale_raw(settings)),
    ):
        total += await _run_domain(name, operation, settings, failures)
    if failures:
        raise ReconcilePartialFailure(f"reconcile domains failed: {len(failures)}") from failures[0]
    return total


async def _replay_stale_raw(settings: Settings) -> int:
    """自动重放租约过期的未处理 raw，避免崩溃后只能等人。"""

    control_url = getattr(settings, "redis_control_url", None)
    if not isinstance(control_url, str) or not control_url:
        return 0
    redis = redis_client(control_url)
    ops = SqlOpsRepository(settings, redis)
    crypto = CryptoService.from_settings(settings)
    alerts = SqlAlertService(settings)
    replay = RawReplayService(
        ops,
        crypto,
        ReportIngestService(None, SqlReportRepository(settings), crypto, alerts=alerts),
        ReplyIngestService(None, SqlReplyRepository(settings), crypto, alerts=alerts),
    )
    replayed = 0
    for raw_id in await ops.list_stale_unprocessed_raw_ids():
        try:
            await replay.replay(
                raw_id,
                actor="system-reconcile",
                ip="127.0.0.1",
                system_producer=True,
            )
            replayed += 1
            continue
        except RawReplayNotFound:
            continue
        except RawReplayConflict:
            pass
        except Exception:
            # 解析/入库失败已由 ingest 侧标记 error 保持可重放；这里只
            # 保证单条毒丸不拖垮整轮对账的其余职责。
            pass
        try:
            if await ops.raw_replay_exhausted(raw_id):
                await alerts.emit(
                    alert_type="raw_replay_exhausted",
                    level="crit",
                    title="raw 自动重放次数已耗尽，需人工重放或排查",
                    detail={"raw_id": raw_id},
                    dedup_key=f"raw_replay_exhausted:{raw_id}",
                )
        except Exception:
            continue
    return replayed


@celery_app.task(name="app.tasks.reconcile")  # type: ignore[untyped-decorator]
@tracked_job("reconcile", expect_interval_s=300)
def reconcile() -> int:
    return run_worker_async(_reconcile())
