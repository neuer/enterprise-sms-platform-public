"""发送流水线的内容准备与后续编排入口。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import (
    ActorPrincipal,
    ApplicationPrincipal,
    SecurityPrincipal,
)
from app.core.bounded_executor import run_bounded
from app.core.sensitive_text import reject_phone_in_text
from app.services.app_ratelimit import ApplicationRateLimiter
from app.services.approval import requires_approval
from app.services.billing import calculate_segments
from app.services.category import CategoryPolicy, coerce_market_dispatch, policy_for_category
from app.services.crypto import CryptoService, EncryptionContext, ProtectedPhone
from app.services.freq import FrequencyLimits
from app.services.idempotency import (
    IdempotencyFingerprint,
    IdempotencyScope,
    usage_request_key,
)
from app.services.masking import mask_phone_text, mask_verify_otp
from app.services.usage_ledger import FrequencyDecisionItem
from app.settings import get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
PHONE_NUMBER = re.compile(r"^1\d{10}$")


def _same_digest(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _canonical_scheduled_at(value: datetime | None) -> str | None:
    """把定时时刻归一为 UTC 瞬时，避免 +08:00 / Z 两种写法打出不同指纹。"""

    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ConsentRequired(ValueError):
    """Web 营销未确认用户同意，对应 CONSENT_REQUIRED/422。"""


class InvalidContent(ValueError):
    """最终下发内容不满足长度约束。"""


class AllFiltered(ValueError):
    """号码经过去重/黑名单/频控后为空，对应 ALL_FILTERED/422。"""


class SensitiveWord(ValueError):
    """内容命中阻断敏感词，对应 SENSITIVE_WORD/422。"""


class IdempotencyConflict(RuntimeError):
    """同一幂等键已用于不同请求，禁止静默复用旧批次。"""


class IdempotencyClaimLost(RuntimeError):
    """幂等临时租约丢失；当前请求必须在进入后续副作用前终止。"""


class VendorTestConsoleOnly(PermissionError):
    """受控真实模式只允许系统配置页的单号码 UAT 入口。"""


class MarketApiBulkForbidden(PermissionError):
    """API 营销大批量未预授权，对应 FORBIDDEN/403。"""


class InFlightLimitExceeded(RuntimeError):
    """单应用在途分片已达上限，对应 RATE_LIMITED/429。"""


class InFlightQueryUnavailable(RuntimeError):
    """在途分片查询失败，必须失败关闭。"""


class QuotaExemptionExpired(RuntimeError):
    """无限额度豁免已到期，不得把 daily_quota=0 继续当作无限。"""


class SendAdmissionPort(Protocol):
    """新发送积压准入；幂等重放不得调用。"""

    async def authorize(
        self,
        *,
        category: str,
        channel: str,
        recipient_count: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedContent:
    send_content: str
    persisted_content: str
    segments: int


def prepare_content(
    *,
    category: str,
    channel: str,
    rendered_content: str,
    sign_name: str | None,
    unsubscribe_suffix: str,
    unsubscribe_auto_append: bool,
    consent_confirmed: bool,
    verify_otp_mask: bool,
) -> PreparedContent:
    """按营销合规→签名→计费→OTP持久化打码的固定顺序准备内容。"""

    if category == "market" and channel == "web" and not consent_confirmed:
        raise ConsentRequired("Web 营销发送必须确认已获用户同意")
    content = rendered_content
    if (
        category == "market"
        and unsubscribe_auto_append
        and unsubscribe_suffix
        and not content.endswith(unsubscribe_suffix)
    ):
        content += unsubscribe_suffix
    send_content = content
    billing_content = f"{sign_name or ''}{content}"
    if not send_content or len(send_content) > 500 or len(billing_content) > 500:
        raise InvalidContent("最终短信内容长度必须在 1 到 500 之间")
    segments = calculate_segments(billing_content)
    display_content = (
        mask_verify_otp(send_content) if category == "verify" and verify_otp_mask else send_content
    )
    persisted = mask_phone_text(display_content)
    return PreparedContent(send_content, persisted, segments)


@dataclass(frozen=True, slots=True)
class SendRequest:
    category: str
    mobiles: Sequence[str]
    content: str | None = None
    template_id: int | None = None
    template_params: Sequence[str] | None = None
    sign_name: str | None = None
    scheduled_at: datetime | None = None
    biz_id: str | None = None
    channel: str = "api"
    consent_confirmed: bool = False
    actor: ActorPrincipal | None = None
    is_test: bool = False
    remark: str | None = None
    resend_of: str | None = None
    resend_dept: str | None = None
    protected_mobiles: Sequence[ProtectedPhone] = ()
    protected_hmac_candidates: Sequence[tuple[int, str]] = ()
    vendor_test_uat: bool = False
    import_reservation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AcceptancePreauthorization:
    """号码解析前已消费的应用限流与类别授权结果。"""

    app_id: int
    category: str
    policy: CategoryPolicy


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    unsubscribe_suffix: str = "回T退订"
    unsubscribe_auto_append: bool = True
    verify_otp_mask: bool = True
    verify_per_minute: int = 1
    verify_per_day: int = 10
    market_per_day: int = 1
    dept_daily_quota: int = 0
    market_window: str = "08:00-21:00"
    sensitive_hit_action: str = "block"
    approval_threshold: int = 100
    market_approval_threshold: int = 50
    approval_expire_hours: int = 24
    test_send_max: int = 5
    max_schedule_ahead_days: int = 90


@dataclass(frozen=True, slots=True)
class BatchCommand:
    batch_no: str
    app_id: int | None
    dept: str
    category: str
    channel: str
    display_content_enc: bytes
    send_content_enc: bytes
    sign_name: str | None
    template_id: int | None
    biz_id: str | None
    segments: int
    quota_cost: int
    status: str
    deferred_reason: str | None
    scheduled_at: datetime | None
    removed_duplicate: int
    removed_blacklist: int
    removed_freq: int
    principal: ActorPrincipal
    approval_expire_hours: int
    approval_threshold: int | None
    is_test: bool
    consent_confirmed: bool
    remark: str | None
    resend_of: str | None
    usage_reservation_id: UUID | None
    import_reservation_id: UUID | None
    messages: tuple[ProtectedPhone, ...]
    scope_kind: str
    scope_id: str
    request_hash: str | None = None
    request_hash_key_version: int | None = None


@dataclass(frozen=True, slots=True)
class StoredBatch:
    batch_no: str
    idempotent: bool
    outbox_persisted: bool = False


@dataclass(frozen=True, slots=True)
class BatchResponse:
    batch_no: str
    idempotent: bool
    accepted: int
    removed_duplicate: int
    removed_blacklist: int
    removed_freq_limit: int
    est_segments: int
    quota_cost: int
    status: str
    deferred_reason: str | None
    scheduled_at: datetime | None
    idempotency_expires_at: datetime | None = None


class PipelineStore(Protocol):
    async def response_for(self, batch_no: str) -> BatchResponse: ...

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]: ...

    async def sensitive_hits(self, content: str) -> list[str]: ...

    async def audit_sensitive_hit(self, app_id: int, hit_count: int) -> None: ...

    async def save(self, command: BatchCommand) -> StoredBatch: ...

    async def count_in_flight_chunks(self, app_id: int) -> int: ...


class IdempotencyPort(Protocol):
    def claim_key(self, scope: IdempotencyScope, biz_id: str) -> str: ...

    def frequency_result_key(
        self, scope: IdempotencyScope, biz_id: str
    ) -> str: ...

    def quota_result_key(
        self, scope: IdempotencyScope, biz_id: str, date_key: str
    ) -> str: ...

    async def request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyFingerprint | None: ...

    async def lookup(
        self, scope: IdempotencyScope, biz_id: str
    ) -> str | None: ...

    async def remember(
        self, scope: IdempotencyScope, biz_id: str, batch_no: str
    ) -> None: ...

    async def claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        fingerprint: str = "",
    ) -> str | None: ...

    async def wait(
        self, scope: IdempotencyScope, biz_id: str
    ) -> str | None: ...

    async def release(
        self, scope: IdempotencyScope, biz_id: str, token: str
    ) -> None: ...

    async def renew(
        self, scope: IdempotencyScope, biz_id: str, token: str
    ) -> bool: ...

    async def heartbeat(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        token: str,
        lost: asyncio.Event,
    ) -> None: ...


class FrequencyPort(Protocol):
    async def allow(
        self,
        category: str,
        *,
        app_id: int,
        phone_hmac: str,
        limits: FrequencyLimits,
        claim_key: str | None = None,
        claim_token: str | None = None,
        result_key: str | None = None,
    ) -> bool: ...


class QuotaPort(Protocol):
    async def reserve(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        app_limit: int,
        dept_limit: int,
        ttl_s: int,
        claim_key: str | None = None,
        claim_token: str | None = None,
        reservation_key: str | None = None,
    ) -> Any: ...

    async def refund(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
    ) -> Any: ...

    async def refund_reservation(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        reservation_key: str,
    ) -> Any: ...


class UsageLedgerPort(Protocol):
    async def start_reservation(
        self,
        *,
        request_key: str,
        app_id: int,
        dept: str,
        category: str,
        now: datetime | None = None,
    ) -> Any: ...

    async def allow_frequency(
        self,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        phone_hmac: str,
        hmac_aliases: dict[int, str],
        limits: FrequencyLimits,
        now: datetime | None = None,
    ) -> bool: ...

    async def allow_frequency_many(
        self,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        items: Sequence[Any],
        limits: FrequencyLimits,
        now: datetime | None = None,
    ) -> list[bool]: ...

    async def reserve_quota(
        self,
        reservation_id: UUID,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        app_limit: int,
        dept_limit: int,
        expires_at: datetime,
    ) -> None: ...

    async def request_release(
        self,
        reservation_id: UUID,
        *,
        event_id: str,
    ) -> bool: ...

    async def request_unlinked_release(
        self,
        reservation_id: UUID,
        *,
        event_id: str,
    ) -> bool: ...


class QueuePublisher(Protocol):
    async def enqueue(self, batch_no: str, queue: str) -> None: ...


class TemplatePort(Protocol):
    async def render(
        self,
        template_id: int,
        params: Sequence[str],
        dept: str,
    ) -> str: ...


class SignPort(Protocol):
    async def resolve(self, name: str) -> str: ...


class RecipientGuard(Protocol):
    """发送受理边界的号码准入检查，不暴露 HMAC 实现。"""

    def require_allowed(self, phones: Sequence[str]) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class SendPipeline:
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

    async def _render(self, request: SendRequest, dept: str) -> str:
        has_content = request.content is not None
        has_template = request.template_id is not None
        if has_content == has_template:
            raise InvalidContent("content 与 template_id 必须且只能提供一个")
        if request.content is not None:
            return request.content
        if self.templates is None or request.template_id is None:
            raise InvalidContent("模板服务不可用")
        return await self.templates.render(
            request.template_id,
            request.template_params or (),
            dept,
        )

    def _protect_plain_phones(
        self,
        phones: Sequence[str],
        *,
        blacklist_required: bool,
    ) -> tuple[
        list[ProtectedPhone],
        dict[str, frozenset[str]],
        dict[str, str],
        dict[str, dict[int, str]],
    ]:
        """同步保护一批明文号码；必须放入有界执行器，避免阻塞事件循环。"""

        protected: list[ProtectedPhone] = []
        candidates_by_active: dict[str, frozenset[str]] = {}
        frequency_hmac_by_active: dict[str, str] = {}
        frequency_aliases_by_active: dict[str, dict[int, str]] = {}
        for phone in phones:
            item = self.crypto.protect_phone(phone)
            protected.append(item)
            aliases = self.crypto.hmac_candidates(phone)
            frequency_hmac_by_active[item.phone_hmac] = aliases[min(aliases)]
            frequency_aliases_by_active[item.phone_hmac] = aliases
            if blacklist_required:
                candidates_by_active[item.phone_hmac] = frozenset(aliases.values())
        return (
            protected,
            candidates_by_active,
            frequency_hmac_by_active,
            frequency_aliases_by_active,
        )

    def _schedule(
        self,
        request: SendRequest,
    ) -> tuple[Literal["queued", "scheduled"], str | None, datetime | None]:
        if request.is_test:
            if request.scheduled_at is not None:
                raise ValueError("测试发送不支持定时投递")
            return "queued", None, None
        if request.category != "market":
            if request.scheduled_at is not None:
                if request.scheduled_at.tzinfo is None or request.scheduled_at.utcoffset() is None:
                    raise ValueError("scheduled_at must include timezone")
                return "scheduled", None, request.scheduled_at
            return "queued", None, None
        return coerce_market_dispatch(
            self.clock(),
            self.config.market_window,
            request.scheduled_at,
        )

    def _request_hash(
        self,
        request: SendRequest,
        app: ApiAppContext,
        policy: CategoryPolicy,
        *,
        key_version: int | None = None,
        normalize: bool = True,
    ) -> str:
        """生成版本化请求 HMAC，覆盖会改变真实短信副作用与作用域的字段。"""

        actor = request.actor
        if isinstance(actor, SecurityPrincipal):
            actor_document = {
                "kind": "human",
                "account_id": actor.account_id,
                "identity_id": actor.identity_id,
            }
        elif isinstance(actor, ApplicationPrincipal):
            actor_document = {"kind": "app", "app_id": actor.app_id}
        elif actor is None:
            actor_document = None
        else:
            actor_document = str(actor)
        fingerprint_key_version = self.crypto.active_version if key_version is None else key_version
        protected_aliases = dict(request.protected_hmac_candidates)
        protected_identity: dict[str, object] | None = None
        if request.protected_mobiles:
            try:
                protected_digest = protected_aliases[fingerprint_key_version]
            except KeyError:
                raise ValueError("加密测试号码缺少幂等指纹版本") from None
            protected_identity = {
                "key_version": fingerprint_key_version,
                "digest": protected_digest,
            }
        document = {
            "app_id": app.app_id,
            "dept": app.dept,
            "actor": actor_document,
            "channel": request.channel,
            "category": request.category,
            "content": request.content,
            "template_id": request.template_id,
            "template_params": list(request.template_params or ()),
            "sign_name": request.sign_name or app.default_sign,
            "scheduled_at": _canonical_scheduled_at(request.scheduled_at)
            if normalize
            else (
                request.scheduled_at.isoformat()
                if request.scheduled_at is not None
                else None
            ),
            "consent_confirmed": request.consent_confirmed,
            "is_test": request.is_test,
            "mobiles": (
                sorted(set(request.mobiles or ()))
                if normalize
                else list(request.mobiles or ())
            ),
            "protected_phone_identity": protected_identity,
            "vendor_test_uat": request.vendor_test_uat,
            "resend_of": request.resend_of,
            "resend_dept": request.resend_dept,
            "policy": {
                "queue": policy.queue,
                "blacklist_required": policy.blacklist_required,
            },
        }
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self.crypto.idempotency_fingerprint(
            canonical,
            key_version=key_version,
        )

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
    def _idempotency_scope(
        request: SendRequest,
        app: ApiAppContext,
    ) -> IdempotencyScope:
        """稳定幂等主体：API=app，Web=稳定账号/身份复合作用域。"""

        if request.biz_id and request.biz_id.startswith("manual-resend:"):
            resolution_id = request.biz_id.split(":")[1]
            return IdempotencyScope("uncertain-resend", resolution_id)
        if request.resend_of is not None:
            return IdempotencyScope("resend", request.resend_of)
        if request.channel == "web":
            if not isinstance(request.actor, SecurityPrincipal):
                raise ValueError("Web 发送必须绑定稳定账号")
            return IdempotencyScope(
                "account",
                f"{request.actor.account_id}:{request.actor.identity_id}",
            )
        return IdempotencyScope("app", str(app.app_id))

    async def _claim_owner(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        fingerprint: str,
    ) -> str | None:
        try:
            return await self.idempotency.claim(
                scope,
                biz_id,
                fingerprint=fingerprint,
            )
        except TypeError:
            return await self.idempotency.claim(scope, biz_id)

    def _validate_schedule(self, request: SendRequest) -> None:
        if request.is_test and request.scheduled_at is not None:
            raise ValueError("测试发送不支持定时投递")
        if request.scheduled_at is None:
            return
        if request.scheduled_at.tzinfo is None or request.scheduled_at.utcoffset() is None:
            raise ValueError("scheduled_at must include timezone")
        now = self.clock()
        if request.scheduled_at <= now:
            raise ValueError("scheduled_at must be in the future")
        horizon = now + timedelta(days=self.config.max_schedule_ahead_days)
        if request.scheduled_at > horizon:
            raise ValueError(
                f"scheduled_at 不能超过 {self.config.max_schedule_ahead_days} 天"
            )

    async def _ensure_same_request(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        request: SendRequest,
        app: ApiAppContext,
        policy: CategoryPolicy,
    ) -> None:
        """用记录绑定的 HMAC 版本复算；旧记录无指纹时沿用原幂等行为。"""

        stored = await self.idempotency.request_fingerprint(scope, biz_id)
        if stored is None:
            raise IdempotencyConflict(
                "同一幂等键缺少请求指纹，拒绝复用，请更换 biz_id"
            )
        try:
            request_hash = self._request_hash(
                request,
                app,
                policy,
                key_version=stored.key_version,
            )
            legacy_hash = self._request_hash(
                request,
                app,
                policy,
                key_version=stored.key_version,
                normalize=False,
            )
        except ValueError:
            # 记录绑定的 HMAC 版本已在轮换中退役：无法证明是同一请求。
            # 若按 400 参数错误返回，调用方最自然的反应是换 biz_id 重发，
            # 恰好击穿幂等要防的重复下发；因此按幂等冲突 409 处理。
            raise IdempotencyConflict(
                "同一幂等键的请求指纹版本已退役，无法验证同请求；"
                "请先查询原批次状态，勿直接更换 biz_id 重发"
            ) from None
        if _same_digest(stored.digest, request_hash) or _same_digest(
            stored.digest,
            legacy_hash,
        ):
            return
        raise IdempotencyConflict(
            "同一幂等键已用于不同请求，请更换 biz_id 或复用原请求"
        )

    @staticmethod
    def _quota_clock(now: datetime) -> tuple[str, int]:
        local = now.astimezone(SHANGHAI)
        next_day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.strftime("%Y%m%d"), max(1, int((next_day - local).total_seconds()))

    async def _authorize_new_send(self, request: SendRequest) -> None:
        if self.admission_guard is None:
            return
        recipient_count = (
            len(request.protected_mobiles)
            if request.protected_mobiles
            else len(request.mobiles)
        )
        await self.admission_guard.authorize(
            category=request.category,
            channel=request.channel,
            recipient_count=max(1, recipient_count),
        )

    async def _consume_request_limit(
        self,
        app: ApiAppContext,
        request: SendRequest,
        preauthorization: AcceptancePreauthorization | None,
    ) -> None:
        if (
            request.channel != "web"
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
        if request.channel == "web" or self.acceptance_limiter is None:
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
        if request.channel == "web":
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
    ) -> None:
        if request.channel == "web":
            return
        estimated = max(1, ceil(recipient_count / 500))
        reserve = getattr(self.store, "reserve_in_flight_chunks", None)
        if reserve is not None:
            try:
                await reserve(app.app_id, estimated, app.max_in_flight_chunks)
            except InFlightLimitExceeded:
                raise
            except InFlightQueryUnavailable:
                raise
            except Exception as exc:
                raise InFlightQueryUnavailable("在途分片预留不可用") from exc
            return
        counter = getattr(self.store, "count_in_flight_chunks", None)
        if counter is None:
            return
        try:
            current = await counter(app.app_id)
        except InFlightQueryUnavailable:
            raise
        except Exception as exc:
            raise InFlightQueryUnavailable("在途分片查询不可用") from exc
        if current + estimated > app.max_in_flight_chunks:
            raise InFlightLimitExceeded("应用在途分片已达上限")

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
    ) -> BatchResponse:
        if self.vendor_test_console_only and not request.vendor_test_uat:
            raise VendorTestConsoleOnly
        biz_id = request.biz_id
        if not biz_id:
            return await self._accept_claimed(
                app,
                request,
                preauthorization=preauthorization,
            )
        idem_scope = self._idempotency_scope(request, app)
        policy = self._resolve_policy(app, request, preauthorization)
        request_hash_key_version = self.crypto.active_version
        request_hash = self._request_hash(
            request,
            app,
            policy,
            key_version=request_hash_key_version,
        )
        existing = await self.idempotency.lookup(idem_scope, biz_id)
        if existing is not None:
            await self._ensure_same_request(idem_scope, biz_id, request, app, policy)
            await self._consume_replay_limit(app)
            return await self.store.response_for(existing)
        inspect = getattr(self.idempotency, "inspect", None)
        viewed = await inspect(idem_scope, biz_id) if inspect is not None else None
        if viewed is not None:
            if getattr(viewed, "fingerprint", "") not in {"", request_hash}:
                raise IdempotencyConflict(
                    "同一幂等键已用于不同请求，请更换 biz_id 或复用原请求"
                )
            await self._consume_replay_limit(app)
            existing = await self.idempotency.wait(idem_scope, biz_id)
            if existing is not None:
                await self._ensure_same_request(idem_scope, biz_id, request, app, policy)
                return await self.store.response_for(existing)
        token = await self._claim_owner(idem_scope, biz_id, request_hash)
        if token is None:
            viewed = await inspect(idem_scope, biz_id) if inspect is not None else None
            if viewed is not None and getattr(viewed, "fingerprint", "") not in {
                "",
                request_hash,
            }:
                raise IdempotencyConflict(
                    "同一幂等键已用于不同请求，请更换 biz_id 或复用原请求"
                )
            await self._consume_replay_limit(app)
            existing = await self.idempotency.wait(idem_scope, biz_id)
            if existing is not None:
                await self._ensure_same_request(idem_scope, biz_id, request, app, policy)
                return await self.store.response_for(existing)
            token = await self._claim_owner(idem_scope, biz_id, request_hash)
        if token is None:
            raise RuntimeError("idempotency coordination unavailable")
        await self._authorize_new_send(request)
        await self._consume_request_limit(app, request, preauthorization)
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self.idempotency.heartbeat(idem_scope, biz_id, token, lost)
        )

        async def check_ownership() -> None:
            if lost.is_set():
                raise IdempotencyClaimLost("idempotency claim lost")
            try:
                owned = await self.idempotency.renew(idem_scope, biz_id, token)
            except Exception:
                lost.set()
                raise IdempotencyClaimLost("idempotency claim unavailable") from None
            if not owned:
                lost.set()
                raise IdempotencyClaimLost("idempotency claim lost")

        try:
            existing = await self.idempotency.lookup(idem_scope, biz_id)
            if existing is not None:
                await self._ensure_same_request(idem_scope, biz_id, request, app, policy)
                return await self.store.response_for(existing)
            await check_ownership()
            return await self._accept_claimed(
                app,
                request,
                preauthorization=preauthorization,
                ownership_check=check_ownership,
                claim_key=self.idempotency.claim_key(idem_scope, biz_id),
                claim_token=token,
                frequency_result_key=self.idempotency.frequency_result_key(
                    idem_scope, biz_id
                ),
                idem_scope=idem_scope,
                request_hash=request_hash,
                request_hash_key_version=request_hash_key_version,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            try:
                await self.idempotency.release(idem_scope, biz_id, token)
            except Exception as exc:
                LOGGER.error(
                    "idempotency claim release unavailable",
                    extra={"app_id": app.app_id, "error_type": type(exc).__name__},
                )

    async def _accept_claimed(
        self,
        app: ApiAppContext,
        request: SendRequest,
        preauthorization: AcceptancePreauthorization | None = None,
        ownership_check: Callable[[], Awaitable[None]] | None = None,
        claim_key: str | None = None,
        claim_token: str | None = None,
        frequency_result_key: str | None = None,
        idem_scope: IdempotencyScope | None = None,
        request_hash: str | None = None,
        request_hash_key_version: int | None = None,
    ) -> BatchResponse:
        reject_phone_in_text(request.remark, field_name="remark")
        if ownership_check is not None:
            await ownership_check()
        if idem_scope is None and request.biz_id:
            idem_scope = self._idempotency_scope(request, app)
        if request.biz_id and idem_scope is None:
            raise RuntimeError("idempotency scope unavailable")
        if request.biz_id and request_hash is None:
            policy = self._resolve_policy(app, request, preauthorization)
            request_hash_key_version = self.crypto.active_version
            request_hash = self._request_hash(
                request,
                app,
                policy,
                key_version=request_hash_key_version,
            )
        if request.biz_id and request_hash_key_version is None:
            raise RuntimeError("idempotency fingerprint key version unavailable")
        self._validate_schedule(request)
        has_plain = bool(request.mobiles)
        has_protected = bool(request.protected_mobiles)
        if has_plain == has_protected:
            raise ValueError("号码来源必须且只能提供一种")
        recipient_limit = 50_000 if request.channel == "web" else 10_000
        recipient_count = len(request.protected_mobiles) if has_protected else len(request.mobiles)
        if not 1 <= recipient_count <= recipient_limit:
            raise ValueError(f"mobiles count must be 1..{recipient_limit}")
        if has_plain and any(PHONE_NUMBER.fullmatch(phone) is None for phone in request.mobiles):
            raise ValueError("手机号格式无效")
        if request.vendor_test_uat and recipient_count != 1:
            raise ValueError("真实联调 UAT 仅允许一个已登记号码")
        if not request.biz_id:
            await self._authorize_new_send(request)
        if has_protected and not request.vendor_test_uat:
            raise ValueError("加密号码只能由真实联调 UAT 使用")
        if request.is_test and recipient_count > self.config.test_send_max:
            raise ValueError(f"测试发送最多{self.config.test_send_max}个号码")
        if self.recipient_guard is not None and has_plain:
            self.recipient_guard.require_allowed(request.mobiles)
        self._enforce_market_api_bulk(app, request, recipient_count)
        if (
            request.channel != "web"
            and preauthorization is None
            and self.acceptance_limiter is not None
            and not request.biz_id
        ):
            await self.acceptance_limiter.check(
                app_id=app.app_id,
                limit_per_minute=app.rate_limit_per_min,
            )
        policy = self._resolve_policy(app, request, preauthorization)
        rendered = await self._render(request, app.dept)
        sign_name = request.sign_name or app.default_sign
        if sign_name is not None and self.signs is not None:
            sign_name = await self.signs.resolve(sign_name)
        prepared = prepare_content(
            category=request.category,
            channel=request.channel,
            rendered_content=rendered,
            sign_name=sign_name,
            unsubscribe_suffix=self.config.unsubscribe_suffix,
            unsubscribe_auto_append=self.config.unsubscribe_auto_append,
            consent_confirmed=request.consent_confirmed,
            verify_otp_mask=self.config.verify_otp_mask,
        )
        self._enforce_quota_exemption(app, request)
        await self._enforce_in_flight(app, request, recipient_count=recipient_count)
        await self._consume_send_cost(
            app,
            request,
            recipient_count=recipient_count,
            segment_count=prepared.segments * recipient_count,
        )
        if ownership_check is not None:
            await ownership_check()
        hits = await self.store.sensitive_hits(prepared.send_content)
        if hits and self.config.sensitive_hit_action == "block":
            raise SensitiveWord("内容命中敏感词")
        if hits and self.config.sensitive_hit_action == "audit":
            await self.store.audit_sensitive_hit(app.app_id, len(hits))
        unique_phones = list(dict.fromkeys(request.mobiles))
        removed_duplicate = len(request.mobiles) - len(unique_phones)
        protected: list[ProtectedPhone] = []
        candidates_by_active: dict[str, frozenset[str]] = {}
        frequency_hmac_by_active: dict[str, str] = {}
        frequency_aliases_by_active: dict[str, dict[int, str]] = {}
        if has_protected:
            protected_source = list(request.protected_mobiles)
            if any(
                not item.phone_enc
                or re.fullmatch(r"[0-9a-f]{64}", item.phone_hmac) is None
                or not item.phone_mask
                or item.key_version < 1
                for item in protected_source
            ):
                raise ValueError("加密测试号码合同无效")
            aliases = dict(request.protected_hmac_candidates)
            if (
                len(aliases) != len(request.protected_hmac_candidates)
                or set(aliases) != self.crypto.hmac_versions
                or any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in aliases.values())
                or aliases.get(protected_source[0].key_version)
                != protected_source[0].phone_hmac
            ):
                raise ValueError("加密测试号码索引合同无效")
            # 受控测试号码来自 vendor_test_recipient；落入 sms_message 前在内存中
            # 重新封装，避免跨表复制同一份合法密文。
            source_phone = self.crypto.decrypt_phone(
                protected_source[0].phone_enc,
                protected_source[0].key_version,
                protected_source[0].phone_hmac,
                table="vendor_test_recipient",
            )
            protected = [self.crypto.protect_phone(source_phone)]
            stable_hmac = aliases[min(aliases)]
            frequency_hmac_by_active[protected[0].phone_hmac] = stable_hmac
            frequency_aliases_by_active[protected[0].phone_hmac] = aliases
            if policy.blacklist_required:
                candidates_by_active = {protected[0].phone_hmac: frozenset(aliases.values())}
        else:
            (
                protected,
                candidates_by_active,
                frequency_hmac_by_active,
                frequency_aliases_by_active,
            ) = await self._protect_plain_phones_batched(
                unique_phones,
                blacklist_required=policy.blacklist_required,
            )
        blocked_candidates = (
            await self.store.blacklisted(
                set().union(*candidates_by_active.values()) if candidates_by_active else set()
            )
            if policy.blacklist_required
            else set()
        )
        blocked_active = {
            active
            for active, candidates in candidates_by_active.items()
            if not candidates.isdisjoint(blocked_candidates)
        }
        after_blacklist = [item for item in protected if item.phone_hmac not in blocked_active]
        if not after_blacklist:
            raise AllFiltered("全部号码已被过滤")
        limits = FrequencyLimits.from_config(
            verify_per_minute=self.config.verify_per_minute,
            verify_per_day=self.config.verify_per_day,
            market_per_day=self.config.market_per_day,
            override=app.freq_override,
        )
        now = self.clock()
        date_key, ttl_s = self._quota_clock(now)
        usage_reservation_id: UUID | None = None
        usage_reservation_reused = False
        if self.usage_ledger is not None:
            request_key = (
                usage_request_key(idem_scope, request.biz_id, date_key)
                if request.biz_id and idem_scope is not None
                else f"acceptance:{uuid4()}"
            )
            usage_reservation = await self.usage_ledger.start_reservation(
                request_key=request_key,
                app_id=app.app_id,
                dept=app.dept,
                category=request.category,
                now=now,
            )
            usage_reservation_id = UUID(str(usage_reservation.reservation_id))
            usage_reservation_reused = bool(getattr(usage_reservation, "reused", False))

        async def release_usage(reason: str) -> None:
            if self.usage_ledger is None or usage_reservation_id is None:
                return
            try:
                await self.usage_ledger.request_unlinked_release(
                    usage_reservation_id,
                    event_id=f"usage:{usage_reservation_id}:{reason}",
                )
            except Exception as exc:
                LOGGER.error(
                    "usage reservation release fact unavailable",
                    extra={
                        "app_id": app.app_id,
                        "reservation_id": str(usage_reservation_id),
                        "error_type": type(exc).__name__,
                    },
                )

        accepted: list[ProtectedPhone] = []
        try:
            if ownership_check is not None:
                await ownership_check()
            frequency_batch = 200
            for offset in range(0, len(after_blacklist), frequency_batch):
                if ownership_check is not None and offset > 0:
                    await ownership_check()
                batch = after_blacklist[offset : offset + frequency_batch]
                if self.usage_ledger is not None and usage_reservation_id is not None:
                    decisions = await self.usage_ledger.allow_frequency_many(
                        usage_reservation_id,
                        request.category,
                        app_id=app.app_id,
                        items=tuple(
                            FrequencyDecisionItem(
                                phone_hmac=frequency_hmac_by_active[item.phone_hmac],
                                hmac_aliases=frequency_aliases_by_active[item.phone_hmac],
                            )
                            for item in batch
                        ),
                        limits=limits,
                        now=now,
                    )
                else:
                    decisions = []
                    for index, item in enumerate(batch):
                        if ownership_check is not None and index > 0 and index % 25 == 0:
                            await ownership_check()
                        decisions.append(
                            await self.frequency.allow(
                                request.category,
                                app_id=app.app_id,
                                phone_hmac=frequency_hmac_by_active[item.phone_hmac],
                                limits=limits,
                                claim_key=claim_key,
                                claim_token=claim_token,
                                result_key=frequency_result_key,
                            )
                        )
                accepted.extend(
                    item for item, allowed in zip(batch, decisions, strict=True) if allowed
                )
        except Exception:
            await release_usage("acceptance-failed")
            raise
        removed_freq = len(after_blacklist) - len(accepted)
        if not accepted:
            await release_usage("all-filtered")
            raise AllFiltered("全部号码已被过滤")
        quota_cost = prepared.segments * len(accepted)
        if request.biz_id and idem_scope is not None:
            quota_reservation_key = (
                self.idempotency.quota_result_key(idem_scope, request.biz_id, date_key)
            )
        else:
            quota_reservation_key = None
        try:
            if ownership_check is not None:
                await ownership_check()
            if self.usage_ledger is not None and usage_reservation_id is not None:
                next_day = (now.astimezone(SHANGHAI) + timedelta(days=1)).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                await self.usage_ledger.reserve_quota(
                    usage_reservation_id,
                    app_id=app.app_id,
                    dept=app.dept,
                    category=request.category,
                    date_key=date_key,
                    cost=quota_cost,
                    app_limit=app.daily_quota,
                    dept_limit=self.config.dept_daily_quota,
                    expires_at=next_day,
                )
                reservation_reused = usage_reservation_reused
            else:
                reservation = await self.quota.reserve(
                    app_id=app.app_id,
                    dept=app.dept,
                    category=request.category,
                    date_key=date_key,
                    cost=quota_cost,
                    app_limit=app.daily_quota,
                    dept_limit=self.config.dept_daily_quota,
                    ttl_s=ttl_s,
                    claim_key=claim_key,
                    claim_token=claim_token,
                    reservation_key=quota_reservation_key,
                )
                reservation_reused = bool(getattr(reservation, "reused", False))
        except Exception:
            await release_usage("acceptance-failed")
            raise

        async def compensate_reservation() -> None:
            if self.usage_ledger is not None:
                await release_usage("acceptance-failed")
                return
            try:
                if quota_reservation_key is not None:
                    await self.quota.refund_reservation(
                        app_id=app.app_id,
                        dept=app.dept,
                        category=request.category,
                        date_key=date_key,
                        cost=quota_cost,
                        reservation_key=quota_reservation_key,
                    )
                else:
                    await self.quota.refund(
                        app_id=app.app_id,
                        dept=app.dept,
                        category=request.category,
                        date_key=date_key,
                        cost=quota_cost,
                    )
            except Exception as exc:
                LOGGER.error(
                    "quota reservation compensation unavailable",
                    extra={"app_id": app.app_id, "error_type": type(exc).__name__},
                )

        save_attempted = False
        try:
            if ownership_check is not None:
                await ownership_check()
            scheduled_status, deferred_reason, scheduled_at = self._schedule(request)
            status: Literal["queued", "scheduled", "pending_approval"] = scheduled_status
            if not request.is_test and requires_approval(
                request.channel,
                request.category,
                len(accepted),
                notice_threshold=self.config.approval_threshold,
                market_threshold=self.config.market_approval_threshold,
            ):
                if not isinstance(request.actor, SecurityPrincipal):
                    raise ValueError("Web 审批申请人不能为空")
                status = "pending_approval"
            approval_threshold = (
                self.config.market_approval_threshold
                if status == "pending_approval" and request.category == "market"
                else self.config.approval_threshold
                if status == "pending_approval"
                else None
            )
            principal = request.actor
            if principal is None and request.channel == "api":
                principal = ApplicationPrincipal(app.app_id, app.name, app.dept)
            if request.channel == "web" and not isinstance(
                principal,
                SecurityPrincipal,
            ):
                raise ValueError("Web 发送必须绑定稳定账号与身份")
            if request.import_reservation_id is not None and (
                request.channel != "web"
                or not isinstance(principal, SecurityPrincipal)
            ):
                raise ValueError("导入包预留只能绑定 Web 稳定主体")
            if not isinstance(principal, (SecurityPrincipal, ApplicationPrincipal)):
                raise ValueError("发送请求必须绑定稳定主体")
            batch_no = uuid4().hex
            command = BatchCommand(
                batch_no=batch_no,
                app_id=(
                    app.app_id if request.channel != "web" or request.vendor_test_uat else None
                ),
                dept=app.dept,
                category=request.category,
                channel=request.channel,
                display_content_enc=self.crypto.encrypt_bound_packed_text(
                    prepared.persisted_content,
                    EncryptionContext(
                        domain="sms-display-content",
                        table="sms_batch",
                        column="display_content_enc",
                        object_id=batch_no,
                    ),
                ),
                send_content_enc=self.crypto.encrypt_bound_packed_text(
                    prepared.send_content,
                    EncryptionContext(
                        domain="sms-content",
                        table="sms_batch",
                        column="send_content_enc",
                        object_id=batch_no,
                    ),
                ),
                sign_name=sign_name,
                template_id=request.template_id,
                biz_id=request.biz_id,
                scope_kind=idem_scope.kind if idem_scope is not None else "app",
                scope_id=idem_scope.id if idem_scope is not None else "",
                segments=prepared.segments,
                quota_cost=quota_cost,
                status=status,
                deferred_reason=deferred_reason,
                scheduled_at=scheduled_at,
                request_hash=request_hash,
                request_hash_key_version=request_hash_key_version,
                removed_duplicate=removed_duplicate,
                removed_blacklist=len(blocked_active),
                removed_freq=removed_freq,
                principal=principal,
                approval_expire_hours=self.config.approval_expire_hours,
                approval_threshold=approval_threshold,
                is_test=request.is_test,
                consent_confirmed=request.consent_confirmed,
                remark=request.remark,
                resend_of=request.resend_of,
                usage_reservation_id=usage_reservation_id,
                import_reservation_id=request.import_reservation_id,
                messages=tuple(accepted),
            )
            if ownership_check is not None:
                await ownership_check()
            save_attempted = True
            stored = await self.store.save(command)
        except Exception as original:
            if save_attempted and request.biz_id:
                assert idem_scope is not None
                try:
                    existing = await self.idempotency.lookup(idem_scope, request.biz_id)
                except Exception:
                    await compensate_reservation()
                    raise original from None
                if existing is not None:
                    try:
                        policy = self._resolve_policy(app, request, preauthorization)
                        await self._ensure_same_request(
                            idem_scope,
                            request.biz_id,
                            request,
                            app,
                            policy,
                        )
                    except Exception:
                        await compensate_reservation()
                        raise
                    if not usage_reservation_reused:
                        await release_usage("idempotent-reuse")
                    return await self.store.response_for(existing)
            await compensate_reservation()
            raise
        if stored.idempotent:
            try:
                if request.biz_id:
                    assert idem_scope is not None
                    policy = self._resolve_policy(app, request, preauthorization)
                    await self._ensure_same_request(
                        idem_scope,
                        request.biz_id,
                        request,
                        app,
                        policy,
                    )
                return await self.store.response_for(stored.batch_no)
            finally:
                if self.usage_ledger is not None:
                    if not usage_reservation_reused:
                        await release_usage("idempotent-reuse")
                elif not reservation_reused:
                    await compensate_reservation()
        if request.biz_id:
            assert idem_scope is not None
            await self.idempotency.remember(idem_scope, request.biz_id, stored.batch_no)
        if status == "queued" and not stored.outbox_persisted:
            if ownership_check is not None:
                await ownership_check()
            await self.publisher.enqueue(stored.batch_no, policy.queue)
        expires_at = None
        if request.biz_id:
            expires_at = (
                scheduled_at + timedelta(days=7)
                if scheduled_at is not None
                else self.clock() + timedelta(hours=24)
            )
        return BatchResponse(
            stored.batch_no,
            False,
            len(accepted),
            removed_duplicate,
            len(blocked_active),
            removed_freq,
            prepared.segments,
            quota_cost,
            status,
            deferred_reason,
            scheduled_at,
            expires_at,
        )

    async def _protect_plain_phones_batched(
        self,
        phones: Sequence[str],
        *,
        blacklist_required: bool,
    ) -> tuple[
        list[ProtectedPhone],
        dict[str, frozenset[str]],
        dict[str, str],
        dict[str, dict[int, str]],
    ]:
        """分批并行加密，避免把万级号码塞进单个 10s 任务。"""

        chunk_size = 1000
        if len(phones) <= chunk_size:
            return await run_bounded(
                self._protect_plain_phones,
                phones,
                blacklist_required=blacklist_required,
                timeout_s=min(60, max(10, ((len(phones) + 499) // 500) * 10)),
            )
        chunks = [phones[index : index + chunk_size] for index in range(0, len(phones), chunk_size)]
        timeout_s = min(60, max(15, ((chunk_size + 499) // 500) * 10))
        protected: list[ProtectedPhone] = []
        candidates_by_active: dict[str, frozenset[str]] = {}
        frequency_hmac_by_active: dict[str, str] = {}
        frequency_aliases_by_active: dict[str, dict[int, str]] = {}
        wave = 4
        for start in range(0, len(chunks), wave):
            parts = await asyncio.gather(
                *[
                    run_bounded(
                        self._protect_plain_phones,
                        chunk,
                        blacklist_required=blacklist_required,
                        timeout_s=timeout_s,
                    )
                    for chunk in chunks[start : start + wave]
                ]
            )
            for (
                chunk_protected,
                chunk_candidates,
                chunk_hmac,
                chunk_aliases,
            ) in parts:
                protected.extend(chunk_protected)
                candidates_by_active.update(chunk_candidates)
                frequency_hmac_by_active.update(chunk_hmac)
                frequency_aliases_by_active.update(chunk_aliases)
        return (
            protected,
            candidates_by_active,
            frequency_hmac_by_active,
            frequency_aliases_by_active,
        )
