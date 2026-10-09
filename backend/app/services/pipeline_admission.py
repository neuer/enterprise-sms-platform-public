"""发送受理的准入：类别策略、应用限流与发送成本、额度豁免、在途分片与营销批量。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import (
    UncertainEffectPrincipal,
)
from app.services.app_ratelimit import ApplicationRateLimiter
from app.services.category import CategoryPolicy, policy_for_category
from app.services.idempotency import (
    uncertain_resend_biz_id,
)
from app.services.pipeline_contracts import (
    AcceptancePreauthorization,
    InFlightLimitExceeded,
    InFlightQueryUnavailable,
    MarketApiBulkForbidden,
    PipelineConfig,
    PipelineStore,
    QuotaExemptionExpired,
    SendAdmissionPort,
    SendRequest,
)
from app.services.send_inflight import InFlightInvariantViolation
from app.settings import get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")


class SendAdmissionMixin:
    """SendPipeline 的准入步骤；端口由 SendPipeline 注入，运行中替换立即生效。"""

    config: PipelineConfig
    store: PipelineStore
    acceptance_limiter: ApplicationRateLimiter | None
    admission_guard: SendAdmissionPort | None

    @staticmethod
    def _resolve_policy(
        app: ApiAppContext,
        request: SendRequest,
        preauthorization: AcceptancePreauthorization | None,
    ) -> CategoryPolicy:
        expected = policy_for_category(
            request.category,
            app.allowed_categories,
            notice_blacklist=app.blacklist_check,
        )
        if preauthorization is None:
            return expected
        if (
            preauthorization.app_id != app.app_id
            or preauthorization.category != request.category
            or preauthorization.policy != expected
        ):
            raise ValueError("应用预授权合同无效")
        return preauthorization.policy

    @staticmethod
    def _is_system_resend(request: SendRequest) -> bool:
        return isinstance(request.actor, UncertainEffectPrincipal)

    @staticmethod
    def _usage_app_id(app: ApiAppContext, request: SendRequest) -> int:
        if request.usage_subject is not None:
            return request.usage_subject.app_id
        return app.app_id

    @staticmethod
    def _usage_dept(app: ApiAppContext, request: SendRequest) -> str:
        if request.usage_subject is not None:
            return request.usage_subject.dept
        return app.dept

    @staticmethod
    def _usage_subject_kind(request: SendRequest) -> str:
        if request.usage_subject is not None:
            return request.usage_subject.kind
        return "api_app"

    @staticmethod
    def _validate_usage_subject(request: SendRequest) -> None:
        if request.usage_subject is None:
            if isinstance(request.actor, UncertainEffectPrincipal):
                raise ValueError("system resend requires usage subject")
            return
        if not isinstance(request.actor, UncertainEffectPrincipal):
            raise ValueError("usage subject is not forgeable")
        actor = request.actor
        usage = request.usage_subject
        if (
            request.biz_id != uncertain_resend_biz_id(actor.resolution_id, actor.effect_generation)
            or usage.resolution_id != actor.resolution_id
            or usage.effect_generation != actor.effect_generation
            or usage.dept != actor.dept
            or usage.category != request.category
        ):
            raise ValueError("system resend principal is not forgeable")

    @staticmethod
    def _quota_clock(now: datetime) -> tuple[str, int]:
        local = now.astimezone(SHANGHAI)
        next_day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.strftime("%Y%m%d"), max(1, int((next_day - local).total_seconds()))

    async def _authorize_new_send(
        self,
        request: SendRequest,
        *,
        estimated_segments: int | None = None,
        estimated_chunks: int | None = None,
    ) -> None:
        if self.admission_guard is None:
            return
        recipient_count = (
            len(request.protected_mobiles) if request.protected_mobiles else len(request.mobiles)
        )
        await self.admission_guard.authorize(
            category=request.category,
            channel=request.channel,
            recipient_count=max(1, recipient_count),
            estimated_segments=estimated_segments,
            estimated_chunks=estimated_chunks,
        )

    async def _consume_request_limit(
        self,
        app: ApiAppContext,
        request: SendRequest,
        preauthorization: AcceptancePreauthorization | None,
    ) -> None:
        if (
            (request.channel != "web" or self._is_system_resend(request))
            and preauthorization is None
            and self.acceptance_limiter is not None
        ):
            await self.acceptance_limiter.check(
                app_id=app.app_id,
                limit_per_minute=app.rate_limit_per_min,
            )

    async def _consume_replay_limit(self, app: ApiAppContext) -> None:
        limiter = self.acceptance_limiter
        if limiter is None:
            return
        replay = getattr(limiter, "check_replay", None)
        if replay is not None:
            await replay(app_id=app.app_id, limit_per_minute=app.rate_limit_per_min)

    async def _consume_send_cost(
        self,
        app: ApiAppContext,
        request: SendRequest,
        *,
        recipient_count: int,
        segment_count: int,
    ) -> None:
        if (
            request.channel == "web" and not self._is_system_resend(request)
        ) or self.acceptance_limiter is None:
            return
        consume = getattr(self.acceptance_limiter, "consume_send_cost", None)
        if consume is None:
            return
        await consume(
            app_id=app.app_id,
            recipient_count=recipient_count,
            segment_count=segment_count,
            recipient_limit=app.recipient_limit_per_min,
            segment_limit=app.segment_limit_per_min,
        )

    def _enforce_quota_exemption(self, app: ApiAppContext, request: SendRequest) -> None:
        if request.channel == "web" and not self._is_system_resend(request):
            return
        if app.daily_quota != 0:
            return
        if get_settings().environment != "production":
            return
        until = app.unlimited_quota_exempt_until
        if until is None or until.tzinfo is None or until <= datetime.now(UTC):
            raise QuotaExemptionExpired("无限额度豁免已到期")

    async def _enforce_in_flight(
        self,
        app: ApiAppContext,
        request: SendRequest,
        *,
        recipient_count: int,
    ) -> Any:
        if request.channel == "web" and not self._is_system_resend(request):
            return None
        batch_size = max(1, int(getattr(self.config, "vendor_batch_size", 500) or 500))
        estimated = max(1, ceil(recipient_count / batch_size))
        reserve = getattr(self.store, "reserve_in_flight_chunks", None)
        if reserve is not None:
            try:
                return await reserve(app.app_id, estimated, app.max_in_flight_chunks)
            except InFlightLimitExceeded:
                raise
            except InFlightQueryUnavailable:
                raise
            except InFlightInvariantViolation:
                raise
            except Exception as exc:
                raise InFlightQueryUnavailable("在途分片预留不可用") from exc
        counter = getattr(self.store, "count_in_flight_chunks", None)
        if counter is None:
            return None
        try:
            current = await counter(app.app_id)
        except InFlightQueryUnavailable:
            raise
        except Exception as exc:
            raise InFlightQueryUnavailable("在途分片查询不可用") from exc
        if current + estimated > app.max_in_flight_chunks:
            raise InFlightLimitExceeded("应用在途分片已达上限")
        return None

    def _enforce_market_api_bulk(
        self,
        app: ApiAppContext,
        request: SendRequest,
        recipient_count: int,
    ) -> None:
        if (
            request.channel == "api"
            and request.category == "market"
            and recipient_count >= self.config.market_approval_threshold
            and not app.allow_market_api_bulk
        ):
            raise MarketApiBulkForbidden("营销大批量 API 发送未预授权")
