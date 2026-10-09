"""发送流水线的内容准备与后续编排入口。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.apikey import ApiAppContext
from app.core.sensitive_text import reject_phone_business_id
from app.services.app_ratelimit import ApplicationRateLimiter
from app.services.category import policy_for_category
from app.services.crypto import CryptoService
from app.services.idempotency import (
    IdempotencyConflict as IdempotencyConflict,
)
from app.services.idempotency import (
    IdempotencyCoordinationTimeout,
    IdempotencyFingerprint,
    IdempotencyScope,
)
from app.services.pipeline_acceptance import (
    PreparedContent,
    _Acceptance,
    prepare_content,
)
from app.services.pipeline_commit import AcceptanceCommitMixin
from app.services.pipeline_contracts import (
    AcceptancePreauthorization,
    AcceptCommitConflict,
    AcceptCommitUnknown,
    AllFiltered,
    BatchCommand,
    BatchResponse,
    ConsentRequired,
    FailedSourceReference,
    FrequencyPort,
    IdempotencyClaimLost,
    IdempotencyPort,
    InFlightLimitExceeded,
    InFlightQueryUnavailable,
    InvalidContent,
    MarketApiBulkForbidden,
    PipelineConfig,
    PipelineStore,
    QueuePublisher,
    QuotaExemptionExpired,
    QuotaPort,
    RecipientGuard,
    SendAdmissionPort,
    SendRequest,
    SensitiveWord,
    SignPort,
    StoredBatch,
    TemplatePort,
    UsageLedgerPort,
    VendorTestConsoleOnly,
)
from app.services.send_inflight import InFlightInvariantViolation as InFlightInvariantViolation

# 契约与准入/幂等步骤已拆到 pipeline_* 模块；调用方继续从这里导入。
__all__ = [
    "AcceptancePreauthorization",
    "AcceptCommitConflict",
    "AcceptCommitUnknown",
    "AllFiltered",
    "ApiAppContext",
    "BatchCommand",
    "BatchResponse",
    "ConsentRequired",
    "FailedSourceReference",
    "IdempotencyClaimLost",
    "IdempotencyConflict",
    "InFlightInvariantViolation",
    "InFlightLimitExceeded",
    "InFlightQueryUnavailable",
    "InvalidContent",
    "MarketApiBulkForbidden",
    "PipelineConfig",
    "prepare_content",
    "PreparedContent",
    "QuotaExemptionExpired",
    "SendPipeline",
    "SendRequest",
    "SensitiveWord",
    "StoredBatch",
    "VendorTestConsoleOnly",
]

def utc_now() -> datetime:
    return datetime.now(UTC)


class SendPipeline(AcceptanceCommitMixin):
    """按规范固定顺序编排同步受理，持久化和外部状态由端口实现。"""

    def __init__(
        self,
        *,
        store: PipelineStore,
        idempotency: IdempotencyPort,
        crypto: CryptoService,
        frequency: FrequencyPort,
        quota: QuotaPort,
        publisher: QueuePublisher,
        config: PipelineConfig,
        templates: TemplatePort | None = None,
        signs: SignPort | None = None,
        recipient_guard: RecipientGuard | None = None,
        vendor_test_console_only: bool = False,
        acceptance_limiter: ApplicationRateLimiter | None = None,
        usage_ledger: UsageLedgerPort | None = None,
        admission_guard: SendAdmissionPort | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.idempotency = idempotency
        self.crypto = crypto
        self.frequency = frequency
        self.quota = quota
        self.publisher = publisher
        self.config = config
        self.templates = templates
        self.signs = signs
        self.recipient_guard = recipient_guard
        self.vendor_test_console_only = vendor_test_console_only
        self.acceptance_limiter = acceptance_limiter
        self.usage_ledger = usage_ledger
        self.admission_guard = admission_guard
        self.clock = clock

    async def response_for(self, batch_no: str) -> BatchResponse:
        """返回已持久化批次；供幂等恢复入口复用同一响应口径。"""

        return await self.store.response_for(batch_no)

    async def replay_if_present(
        self,
        app: ApiAppContext,
        request: SendRequest,
    ) -> BatchResponse | None:
        """只读取已存在幂等结果，不消费新发送准入或普通请求额度。"""

        request = self._with_uat_replay_identity(request)
        biz_id = request.biz_id
        reject_phone_business_id(biz_id, field_name="biz_id")
        if not biz_id:
            return None
        idem_scope = self._idempotency_scope(request, app)
        policy = self._resolve_policy(app, request, None)
        existing = await self.idempotency.lookup(idem_scope, biz_id)
        if existing is not None:
            await self._ensure_same_request(idem_scope, biz_id, request, app, policy)
            await self._consume_replay_limit(app)
            return await self.store.response_for(existing)
        inspect = getattr(self.idempotency, "inspect", None)
        viewed = await inspect(idem_scope, biz_id) if inspect is not None else None
        if viewed is None:
            return None
        request_hash = self._request_hash(
            request,
            app,
            policy,
            key_version=self.crypto.active_version,
        )
        if getattr(viewed, "fingerprint", "") not in {"", request_hash}:
            raise IdempotencyConflict("同一幂等键已用于不同请求，请更换 biz_id 或复用原请求")
        await self._consume_replay_limit(app)
        waited = await self.idempotency.wait(idem_scope, biz_id)
        if waited is None:
            return None
        await self._ensure_same_request(idem_scope, biz_id, request, app, policy)
        return await self.store.response_for(waited)

    async def preauthorize(
        self,
        app: ApiAppContext,
        category: str,
    ) -> AcceptancePreauthorization:
        """在受控号码解析前消费应用限流并验证类别权限。"""

        if self.admission_guard is not None:
            await self.admission_guard.authorize(
                category=category,
                channel="api",
                recipient_count=1,
            )
        if self.acceptance_limiter is not None:
            await self.acceptance_limiter.check(
                app_id=app.app_id,
                limit_per_minute=app.rate_limit_per_min,
            )
        return AcceptancePreauthorization(
            app_id=app.app_id,
            category=category,
            policy=policy_for_category(
                category,
                app.allowed_categories,
                notice_blacklist=app.blacklist_check,
            ),
        )

    async def accept(
        self,
        app: ApiAppContext,
        request: SendRequest,
        *,
        preauthorization: AcceptancePreauthorization | None = None,
        fingerprint_key_version: int | None = None,
    ) -> BatchResponse:
        if self.vendor_test_console_only and not request.vendor_test_uat:
            raise VendorTestConsoleOnly
        self._validate_usage_subject(request)
        biz_id = request.biz_id
        reject_phone_business_id(biz_id, field_name="biz_id")
        if not biz_id:
            return await self._accept_claimed(
                app,
                request,
                preauthorization=preauthorization,
            )
        idem_scope = self._idempotency_scope(request, app)
        policy = self._resolve_policy(app, request, preauthorization)
        request_hash_key_version = (
            self.crypto.active_version
            if fingerprint_key_version is None
            else fingerprint_key_version
        )
        request_hash = self._request_hash(
            request,
            app,
            policy,
            key_version=request_hash_key_version,
        )
        existing = await self.idempotency.lookup(idem_scope, biz_id)
        if existing is not None:
            await self._ensure_same_request(
                idem_scope,
                biz_id,
                request,
                app,
                policy,
                computed=IdempotencyFingerprint(request_hash, request_hash_key_version),
            )
            await self._consume_replay_limit(app)
            return await self.store.response_for(existing)
        inspect = getattr(self.idempotency, "inspect", None)
        viewed = await inspect(idem_scope, biz_id) if inspect is not None else None
        if viewed is not None:
            if getattr(viewed, "fingerprint", "") not in {"", request_hash}:
                raise IdempotencyConflict("同一幂等键已用于不同请求，请更换 biz_id 或复用原请求")
            await self._consume_replay_limit(app)
            existing = await self.idempotency.wait(idem_scope, biz_id)
            if existing is not None:
                await self._ensure_same_request(
                    idem_scope,
                    biz_id,
                    request,
                    app,
                    policy,
                    computed=IdempotencyFingerprint(request_hash, request_hash_key_version),
                )
                return await self.store.response_for(existing)
        token = await self._claim_owner(idem_scope, biz_id, request_hash)
        if token is None:
            viewed = await inspect(idem_scope, biz_id) if inspect is not None else None
            if viewed is not None and getattr(viewed, "fingerprint", "") not in {
                "",
                request_hash,
            }:
                raise IdempotencyConflict("同一幂等键已用于不同请求，请更换 biz_id 或复用原请求")
            await self._consume_replay_limit(app)
            existing = await self.idempotency.wait(idem_scope, biz_id)
            if existing is not None:
                await self._ensure_same_request(
                    idem_scope,
                    biz_id,
                    request,
                    app,
                    policy,
                    computed=IdempotencyFingerprint(request_hash, request_hash_key_version),
                )
                return await self.store.response_for(existing)
            token = await self._claim_owner(idem_scope, biz_id, request_hash)
        if token is None:
            raise IdempotencyCoordinationTimeout("幂等协调暂不可用，请保留原业务键重试")
        lost = asyncio.Event()
        heartbeat: asyncio.Task[None] | None = None

        async def check_ownership() -> None:
            if lost.is_set():
                raise IdempotencyClaimLost("idempotency claim lost")
            try:
                renewer = getattr(self.idempotency, "renew_if_due", self.idempotency.renew)
                owned = await renewer(idem_scope, biz_id, token)
            except Exception:
                lost.set()
                raise IdempotencyClaimLost("idempotency claim unavailable") from None
            if not owned:
                lost.set()
                raise IdempotencyClaimLost("idempotency claim lost")

        try:
            if preauthorization is None:
                await self._authorize_new_send(request)
            await self._consume_request_limit(app, request, preauthorization)
            renewal = self.idempotency.heartbeat(idem_scope, biz_id, token, lost)
            try:
                heartbeat = asyncio.create_task(renewal)
            except BaseException:
                renewal.close()
                raise
            existing = await self.idempotency.lookup(idem_scope, biz_id)
            if existing is not None:
                await self._ensure_same_request(
                    idem_scope,
                    biz_id,
                    request,
                    app,
                    policy,
                    computed=IdempotencyFingerprint(request_hash, request_hash_key_version),
                )
                return await self.store.response_for(existing)
            await check_ownership()
            viewed = await inspect(idem_scope, biz_id) if inspect is not None else None
            fence_token = (
                f"{viewed.token}:{viewed.fingerprint}:{viewed.generation}"
                if viewed is not None
                else token
            )
            return await self._accept_claimed(
                app,
                request,
                preauthorization=preauthorization,
                ownership_check=check_ownership,
                claim_key=self.idempotency.claim_key(idem_scope, biz_id),
                claim_token=fence_token,
                claim_generation=viewed.generation if viewed is not None else 1,
                frequency_result_key=self.idempotency.frequency_result_key(idem_scope, biz_id),
                idem_scope=idem_scope,
                request_hash=request_hash,
                request_hash_key_version=request_hash_key_version,
            )
        finally:
            original_error = sys.exception()
            lost.set()
            # 只 shield 当前请求拥有的有界清理，等待其结束，绝不遗留后台释放任务。
            cleanup = asyncio.get_running_loop().create_task(
                self._cleanup_claim(heartbeat, idem_scope, biz_id, token, app.app_id)
            )
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled and original_error is None:
                raise asyncio.CancelledError

    async def _accept_claimed(
        self,
        app: ApiAppContext,
        request: SendRequest,
        preauthorization: AcceptancePreauthorization | None = None,
        ownership_check: Callable[[], Awaitable[None]] | None = None,
        claim_key: str | None = None,
        claim_token: str | None = None,
        claim_generation: int | None = None,
        frequency_result_key: str | None = None,
        idem_scope: IdempotencyScope | None = None,
        request_hash: str | None = None,
        request_hash_key_version: int | None = None,
    ) -> BatchResponse:
        """按固定阶段编排一次受理；任一阶段失败时按绑定状态释放未绑定的在途预留。

        白名单校验在 _validate_acceptance 中，必须早于 _screen_recipients 的号码保护。
        """

        acceptance = _Acceptance(
            app=app,
            request=request,
            preauthorization=preauthorization,
            ownership_check=ownership_check,
            claim_key=claim_key,
            claim_token=claim_token,
            claim_generation=claim_generation,
            frequency_result_key=frequency_result_key,
            idem_scope=idem_scope,
            request_hash=request_hash,
            request_hash_key_version=request_hash_key_version,
        )
        await self._validate_acceptance(acceptance)
        policy, prepared, sign_name = await self._prepare_acceptance(acceptance)
        acceptance.inflight = await self._enforce_in_flight(
            app, request, recipient_count=acceptance.recipient_count
        )
        try:
            screened = await self._screen_recipients(acceptance, policy, prepared)
            window = await self._start_usage_reservation(acceptance)
            accepted = await self._apply_frequency(acceptance, screened, window)
            hold = await self._reserve_quota(acceptance, prepared, accepted, window)
            persisted = await self._persist_batch(
                acceptance, prepared, sign_name, screened, accepted, hold
            )
            if isinstance(persisted, BatchResponse):
                return persisted
            return await self._finish_acceptance(
                acceptance, policy, prepared, screened, accepted, hold, persisted
            )
        finally:
            if acceptance.allow_unbound_release and not acceptance.inflight_bound:
                await self._release_inflight(acceptance, "acceptance-failed")

