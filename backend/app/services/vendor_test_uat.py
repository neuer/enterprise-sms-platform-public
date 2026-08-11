"""系统配置页单号码真实 UAT 的安全编排。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any, Protocol

from redis.asyncio import Redis

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import SecurityPrincipal
from app.services.app_ratelimit import ApplicationRateLimiter
from app.services.app_repository import SqlAppRepository
from app.services.billing_preview import BillingPreview, build_billing_preview
from app.services.crypto import CryptoService, ProtectedPhone
from app.services.freq import FrequencyLimiter
from app.services.idempotency import IdempotencyCoordinator
from app.services.pipeline import BatchResponse, PipelineConfig, SendPipeline, SendRequest
from app.services.pipeline_repository import SqlPipelineStore, SqlTemplateRenderer
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaService
from app.services.runtime_policy import RuntimePolicy
from app.services.sign import SignResolver
from app.services.sign_repository import SqlSignRepository
from app.services.usage_ledger import UsageLedgerService
from app.services.vendor_test_operation import (
    UatBatchResult,
    VendorTestOperation,
    VendorTestOperationConflict,
    VendorTestOperationPending,
    vendor_test_uat_biz_id,
)
from app.services.vendor_test_operation_repository import SqlVendorTestOperationRepository
from app.services.vendor_test_recipient import (
    RecipientBusy,
    RecipientHmacIndexStale,
    RecipientNotFound,
    VendorTestRecipientForSend,
    VendorTestRecipientService,
)
from app.services.vendor_test_recipient_repository import (
    SqlVendorTestRecipientRepository,
)
from app.settings import get_settings

LOGGER = logging.getLogger(__name__)


class VendorTestAppUnavailable(LookupError):
    """UAT 指定应用不存在、已停用或配置不可安全使用。"""


class UatOperationRepository(Protocol):
    async def reserve_start(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        conflicting_types: frozenset[str],
    ) -> VendorTestOperation: ...

    async def mark_running(self, operation_id: str) -> VendorTestOperation: ...

    async def claim_uat_running(
        self,
        operation_id: str,
    ) -> VendorTestOperation | None: ...

    def acceptance_guard(
        self,
        operation_id: str,
    ) -> AbstractAsyncContextManager[None]: ...

    async def prepare_uat_acceptance(self, operation_id: str) -> bool: ...

    async def heartbeat(self, operation_id: str) -> bool: ...

    async def attach_batch(
        self,
        operation_id: str,
        *,
        batch_no: str,
    ) -> VendorTestOperation: ...

    async def complete(
        self,
        operation_id: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
        batch_no: str | None = None,
    ) -> VendorTestOperation: ...

    async def get(self, operation_id: str) -> VendorTestOperation | None: ...

    async def pending(self) -> tuple[VendorTestOperation, ...]: ...

    async def uat_result(
        self,
        operation_id: str,
        *,
        batch_no: str | None,
    ) -> UatBatchResult | None: ...

    async def expire_uat_if_stale(
        self,
        operation_id: str,
        *,
        safe_code: str,
    ) -> VendorTestOperation | None: ...


class UatRecipientRepository(Protocol):
    async def resolve_for_send(self, recipient_id: int) -> VendorTestRecipientForSend: ...


class UatAppRepository(Protocol):
    async def get(self, app_id: int) -> dict[str, Any] | None: ...


class UatPreviewConfigStore(Protocol):
    async def load_config(self, dept: str) -> dict[str, str]: ...


class UatPreviewRenderer(Protocol):
    async def render(
        self,
        template_id: int,
        params: Sequence[str],
        dept: str,
    ) -> str: ...


class UatPreviewSigns(Protocol):
    async def resolve(self, name: str) -> str: ...


PipelineFactory = Callable[
    [ApiAppContext],
    AbstractAsyncContextManager[SendPipeline],
]
Sleeper = Callable[[float], Awaitable[None]]


class VendorTestUatReconciler:
    """只读取 PostgreSQL 事实源恢复 UAT，永不持有 pipeline 或发送入口。"""

    def __init__(self, operations: UatOperationRepository) -> None:
        self.operations = operations

    async def recover(
        self,
        record: VendorTestOperation,
    ) -> tuple[VendorTestOperation, bool]:
        if (
            record.operation_type != "uat_send"
            or record.status not in {"requested", "running"}
        ):
            return record, False
        result = await self.operations.uat_result(
            record.operation_id,
            batch_no=record.batch_no,
        )
        if result is None:
            expired = await self.operations.expire_uat_if_stale(
                record.operation_id,
                safe_code="UAT_ACCEPTANCE_EXPIRED",
            )
            if expired is not None:
                return expired, True
            # accept 可能刚在 guard 释放前提交；重新读取一次只做引用恢复。
            result = await self.operations.uat_result(
                record.operation_id,
                batch_no=record.batch_no,
            )
            if result is None:
                return record, False
        changed = False
        if record.batch_no is None:
            record = await self.operations.attach_batch(
                record.operation_id,
                batch_no=result.batch_no,
            )
            changed = True
        if result.status == "running":
            return record, changed
        record = await self.operations.complete(
            record.operation_id,
            status=result.status,
            safe_code=result.safe_code,
            vendor_code=result.vendor_code,
            batch_no=result.batch_no,
        )
        return record, True

    async def reconcile_once(self) -> int:
        reconciled = 0
        for record in await self.operations.pending():
            _, changed = await self.recover(record)
            reconciled += int(changed)
        return reconciled


class VendorTestUatService:
    """operation 只记安全元数据，API 到 pipeline 全程传递受保护号码。"""

    def __init__(
        self,
        operations: UatOperationRepository,
        recipients: UatRecipientRepository,
        apps: UatAppRepository,
        pipeline_factory: PipelineFactory,
        *,
        heartbeat_interval_s: float = 10,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not 0 < heartbeat_interval_s < 60:
            raise ValueError("UAT heartbeat interval must be within lease")
        self.operations = operations
        self.recipients = recipients
        self.apps = apps
        self.pipeline_factory = pipeline_factory
        self.heartbeat_interval_s = heartbeat_interval_s
        self.sleeper = sleeper

    @staticmethod
    def _app(row: dict[str, Any] | None, expected_id: int) -> ApiAppContext:
        if row is None or row.get("status") != 1 or row.get("id") != expected_id:
            raise VendorTestAppUnavailable("UAT 应用不存在或已停用")
        categories = row.get("allowed_categories")
        if not isinstance(categories, list) or not categories:
            raise VendorTestAppUnavailable("UAT 应用类别配置无效")
        allowed = frozenset(str(item) for item in categories)
        if not allowed.issubset({"verify", "notice", "market"}):
            raise VendorTestAppUnavailable("UAT 应用类别配置无效")
        try:
            return ApiAppContext(
                app_id=int(row["id"]),
                name=str(row["name"]),
                dept=str(row["dept"]),
                allowed_categories=allowed,
                default_sign=(
                    str(row["default_sign"])
                    if row.get("default_sign") is not None
                    else None
                ),
                daily_quota=int(row.get("daily_quota", 0)),
                blacklist_check=bool(row.get("blacklist_check", True)),
                freq_override=row.get("freq_override"),
                rate_limit_per_min=int(row.get("rate_limit_per_min", 60)),
            )
        except (KeyError, TypeError, ValueError):
            raise VendorTestAppUnavailable("UAT 应用配置无效") from None

    @staticmethod
    def _safe_failure(error: Exception) -> str:
        if isinstance(error, RecipientNotFound):
            return "RECIPIENT_NOT_AVAILABLE"
        if isinstance(error, RecipientHmacIndexStale):
            return "RECIPIENT_INDEX_STALE"
        if isinstance(error, VendorTestAppUnavailable):
            return "APP_NOT_AVAILABLE"
        return "UAT_SEND_REJECTED"

    async def _heartbeat(self, operation_id: str) -> None:
        """只续租当前 pre-batch operation；失败时由 guard/对账决定终态。"""

        while True:
            await self.sleeper(self.heartbeat_interval_s)
            try:
                renewed = await self.operations.heartbeat(operation_id)
            except Exception as error:
                LOGGER.warning(
                    "vendor UAT operation heartbeat unavailable",
                    extra={
                        "operation_id": operation_id,
                        "error_type": type(error).__name__,
                    },
                )
                return
            if not renewed:
                return

    async def send(
        self,
        *,
        operation_id: str,
        recipient_id: int,
        app_id: int,
        category: str,
        principal: SecurityPrincipal,
        content: str | None,
        template_id: int | None,
        template_params: Sequence[str] | None,
        sign_name: str | None,
        consent_confirmed: bool,
        remark: str | None,
    ) -> VendorTestOperation:
        try:
            await self.operations.reserve_start(
                operation_id,
                "uat_send",
                principal=principal,
                conflicting_types=frozenset(
                    {"uat_send", "reset_configuration"}
                ),
            )
        except VendorTestOperationConflict:
            raise RecipientBusy("已有真实联调操作正在执行") from None
        record = await self.operations.claim_uat_running(operation_id)
        if record is None:
            current = await self.operations.get(operation_id)
            if current is None or current.operation_type != "uat_send":
                raise VendorTestOperationConflict("UAT operation 状态冲突")
            return (await VendorTestUatReconciler(self.operations).recover(current))[0]
        heartbeat = asyncio.create_task(self._heartbeat(operation_id))
        accept_started = False
        try:
            recipient = await self.recipients.resolve_for_send(recipient_id)
            app = self._app(await self.apps.get(app_id), app_id)
            async with self.operations.acceptance_guard(operation_id):
                if not await self.operations.prepare_uat_acceptance(operation_id):
                    # 等待 guard 期间 lease 可能已经被对账关闭，或其他恢复器
                    # 已附着 batch；此后只能读 PostgreSQL 事实源，绝不能 accept。
                    accept_started = True
                    current = await self.operations.get(operation_id)
                    if current is None or current.operation_type != "uat_send":
                        raise VendorTestOperationConflict("UAT operation 状态冲突")
                    return (
                        await VendorTestUatReconciler(self.operations).recover(current)
                    )[0]
                async with self.pipeline_factory(app) as pipeline:
                    accept_started = True
                    response: BatchResponse = await pipeline.accept(
                        app,
                        SendRequest(
                            category=category,
                            mobiles=(),
                            content=content,
                            template_id=template_id,
                            template_params=template_params,
                            sign_name=sign_name,
                            channel="web",
                            consent_confirmed=consent_confirmed,
                            actor=principal,
                            is_test=True,
                            remark=remark,
                            biz_id=vendor_test_uat_biz_id(operation_id),
                            protected_mobiles=(
                                ProtectedPhone(
                                    phone_enc=recipient.phone_enc,
                                    phone_hmac=recipient.phone_hmac,
                                    phone_mask=recipient.phone_mask,
                                    key_version=recipient.key_version,
                                ),
                            ),
                            protected_hmac_candidates=recipient.hmac_candidates,
                            vendor_test_uat=True,
                        ),
                    )
                return await self.operations.attach_batch(
                    operation_id,
                    batch_no=response.batch_no,
                )
        except Exception as error:
            if accept_started:
                raise VendorTestOperationPending(
                    "UAT 提交结果等待 PostgreSQL 事实源对账"
                ) from None
            await self.operations.complete(
                operation_id,
                status="failed",
                safe_code=self._safe_failure(error),
            )
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def get(self, operation_id: str) -> VendorTestOperation | None:
        record = await self.operations.get(operation_id)
        if (
            record is None
            or record.operation_type != "uat_send"
            or record.status not in {"requested", "running"}
        ):
            return record
        return (await VendorTestUatReconciler(self.operations).recover(record))[0]


class VendorTestUatPreviewService:
    """按所选应用部门复用服务端模板、签名、合规与计费口径。"""

    def __init__(
        self,
        apps: UatAppRepository,
        config_store: UatPreviewConfigStore,
        templates: UatPreviewRenderer,
        signs: UatPreviewSigns,
    ) -> None:
        self.apps = apps
        self.config_store = config_store
        self.templates = templates
        self.signs = signs

    async def preview(
        self,
        *,
        app_id: int,
        category: str,
        content: str | None,
        template_id: int | None,
        template_params: Sequence[str] | None,
        sign_name: str | None,
        consent_confirmed: bool,
    ) -> BillingPreview:
        app = VendorTestUatService._app(await self.apps.get(app_id), app_id)
        if content is not None:
            rendered = content
        elif template_id is not None:
            rendered = await self.templates.render(
                template_id,
                template_params or (),
                app.dept,
            )
        else:
            raise ValueError("content 与 template_id 必须且只能提供一个")
        resolved_sign = sign_name or app.default_sign
        if resolved_sign is not None:
            resolved_sign = await self.signs.resolve(resolved_sign)
        values = await self.config_store.load_config(app.dept)
        policy = RuntimePolicy.from_mapping(values)
        return build_billing_preview(
            category=category,
            content=rendered,
            sign_name=resolved_sign,
            accepted_count=1,
            consent_confirmed=consent_confirmed,
            unsubscribe_suffix=policy.unsubscribe_suffix,
            unsubscribe_auto_append=policy.unsubscribe_auto_append,
            verify_otp_mask=policy.verify_otp_mask,
            notice_threshold=policy.approval_threshold,
            market_threshold=policy.market_approval_threshold,
        )
def build_vendor_test_uat_service() -> VendorTestUatService:
    """组装既有模板、合规、计费、频控、配额、队列与 callback 关联链路。"""

    settings = get_settings()
    crypto = CryptoService.from_settings(settings)

    @asynccontextmanager
    async def pipeline_factory(app: ApiAppContext) -> AsyncIterator[SendPipeline]:
        store = SqlPipelineStore(settings)
        values = await store.load_config(app.dept)
        policy = RuntimePolicy.from_mapping(values)
        redis: Any = Redis.from_url(settings.redis_control_url, decode_responses=True)
        try:
            yield SendPipeline(
                store=store,
                idempotency=IdempotencyCoordinator(redis, store),
                crypto=crypto,
                frequency=FrequencyLimiter(redis),
                quota=QuotaService(redis),
                publisher=CeleryQueuePublisher(),
                templates=SqlTemplateRenderer(settings),
                signs=SignResolver(SqlSignRepository(settings)),
                vendor_test_console_only=True,
                acceptance_limiter=ApplicationRateLimiter(redis),
                usage_ledger=UsageLedgerService(redis, settings),
                config=PipelineConfig(
                    unsubscribe_suffix=policy.unsubscribe_suffix,
                    unsubscribe_auto_append=policy.unsubscribe_auto_append,
                    verify_otp_mask=policy.verify_otp_mask,
                    verify_per_minute=policy.verify_freq_per_minute,
                    verify_per_day=policy.verify_freq_per_day,
                    market_per_day=policy.market_freq_per_day,
                    dept_daily_quota=int(values.get("dept_daily_quota", "0")),
                    market_window=policy.market_send_window,
                    sensitive_hit_action=policy.sensitive_hit_action,
                    approval_threshold=policy.approval_threshold,
                    market_approval_threshold=policy.market_approval_threshold,
                    approval_expire_hours=policy.approval_expire_hours,
                    test_send_max=policy.test_send_max,
                ),
            )
        finally:
            await redis.aclose()

    return VendorTestUatService(
        SqlVendorTestOperationRepository(settings),
        VendorTestRecipientService(
            SqlVendorTestRecipientRepository(settings),
            crypto,
        ),
        SqlAppRepository(settings),
        pipeline_factory,
    )


def build_vendor_test_uat_preview_service() -> VendorTestUatPreviewService:
    settings = get_settings()
    return VendorTestUatPreviewService(
        SqlAppRepository(settings),
        SqlPipelineStore(settings),
        SqlTemplateRenderer(settings),
        SignResolver(SqlSignRepository(settings)),
    )
