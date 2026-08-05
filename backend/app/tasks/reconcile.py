"""uncertain 证据对账任务；后续 M2 在此扩展队列兜底重投。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.crypto import CryptoService
from app.services.queue import CeleryQueuePublisher
from app.services.reconcile import RecoveryReconciler
from app.services.reconcile_repository import SqlRecoveryRepository
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
from app.settings import get_settings
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
    return uncertain + recovered + control_operations + uat_operations


@celery_app.task(name="app.tasks.reconcile")  # type: ignore[untyped-decorator]
@tracked_job("reconcile", expect_interval_s=300)
def reconcile() -> int:
    return run_worker_async(_reconcile())
