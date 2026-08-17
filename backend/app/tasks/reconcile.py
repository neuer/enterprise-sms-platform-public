"""uncertain 证据对账任务；后续 M2 在此扩展队列兜底重投。"""

from __future__ import annotations

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


async def _reconcile() -> int:
    settings = get_settings()
    policy = await SqlRuntimePolicyLoader(settings).load()
    uncertain = await UncertainReconciler.from_policy(
        SqlUncertainRepository(settings),
        CryptoService.from_settings(settings),
        policy,
    ).run_once()
    recovered = await RecoveryReconciler(
        SqlRecoveryRepository(settings),
        CeleryQueuePublisher(),
    ).run_once()
    operation_repository = SqlVendorTestOperationRepository(settings)
    control_operations = await VendorTestOperationService(
        operation_repository,
        VendorControlClient(),
        finalizers={
            "reset_configuration": VendorTestResetFinalizer(
                SqlVendorTestRecipientRepository(settings)
            ),
        },
    ).reconcile_once()
    uat_operations = await VendorTestUatReconciler(
        operation_repository,
    ).reconcile_once()
    replayed = await _replay_stale_raw(settings)
    return uncertain + recovered + control_operations + uat_operations + replayed


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
            await replay.replay(raw_id, actor="system-reconcile", ip="127.0.0.1")
            replayed += 1
        except (RawReplayNotFound, RawReplayConflict):
            continue
    return replayed


@celery_app.task(name="app.tasks.reconcile")  # type: ignore[untyped-decorator]
@tracked_job("reconcile", expect_interval_s=300)
def reconcile() -> int:
    return run_worker_async(_reconcile())
