"""发送流水线的内容准备与后续编排入口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
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
from app.services.app_ratelimit import ApplicationRateLimiter
from app.services.approval import requires_approval
from app.services.billing import calculate_segments
from app.services.category import CategoryPolicy, policy_for_category
from app.services.crypto import CryptoService, EncryptionContext, ProtectedPhone
from app.services.freq import FrequencyLimits
from app.services.masking import mask_phone_text, mask_verify_otp

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
PHONE_NUMBER = re.compile(r"^1\d{10}$")


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
        and unsubscribe_suffix not in content
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
    persisted_content: str
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
    request_hash: str | None = None


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


class PipelineStore(Protocol):
    async def response_for(self, batch_no: str) -> BatchResponse: ...

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]: ...

    async def sensitive_hits(self, content: str) -> list[str]: ...

    async def audit_sensitive_hit(self, app_id: int, hit_count: int) -> None: ...

    async def save(self, command: BatchCommand) -> StoredBatch: ...


class IdempotencyPort(Protocol):
    def claim_key(self, app_id: int | None, biz_id: str) -> str: ...

    def frequency_result_key(self, app_id: int | None, biz_id: str) -> str: ...

    def quota_result_key(self, app_id: int | None, biz_id: str, date_key: str) -> str: ...

    async def request_hash(self, app_id: int | None, biz_id: str) -> str | None: ...

    async def lookup(self, app_id: int | None, biz_id: str) -> str | None: ...

    async def remember(self, app_id: int | None, biz_id: str, batch_no: str) -> None: ...

    async def claim(self, app_id: int | None, biz_id: str) -> str | None: ...

    async def wait(self, app_id: int | None, biz_id: str) -> str | None: ...

    async def release(self, app_id: int | None, biz_id: str, token: str) -> None: ...

    async def renew(self, app_id: int | None, biz_id: str, token: str) -> bool: ...

    async def heartbeat(
        self,
        app_id: int | None,
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


class QueuePublisher(Protocol):
    async def enqueue(self, batch_no: str, queue: str) -> None: ...


class TemplatePort(Protocol):
    async def render(self, template_id: int, params: Sequence[str]) -> str: ...


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
        self.clock = clock

    async def response_for(self, batch_no: str) -> BatchResponse:
        """返回已持久化批次；供幂等恢复入口复用同一响应口径。"""

        return await self.store.response_for(batch_no)

    async def _render(self, request: SendRequest) -> str:
        has_content = request.content is not None
        has_template = request.template_id is not None
        if has_content == has_template:
            raise InvalidContent("content 与 template_id 必须且只能提供一个")
        if request.content is not None:
            return request.content
        if self.templates is None or request.template_id is None:
            raise InvalidContent("模板服务不可用")
        return await self.templates.render(request.template_id, request.template_params or ())

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
            return "queued", None, None
        if request.scheduled_at is not None:
            if request.scheduled_at.tzinfo is None or request.scheduled_at.utcoffset() is None:
                raise ValueError("scheduled_at must include timezone")
            return "scheduled", None, request.scheduled_at
        if request.category != "market":
            return "queued", None, None
        start_raw, end_raw = self.config.market_window.split("-", maxsplit=1)
        start = time.fromisoformat(start_raw)
        end = time.fromisoformat(end_raw)
        local = self.clock().astimezone(SHANGHAI)
        if start <= local.time().replace(tzinfo=None) < end:
            return "queued", None, None
        target_date = (
            local.date()
            if local.time().replace(tzinfo=None) < start
            else local.date() + timedelta(days=1)
        )
        target = datetime.combine(target_date, start, tzinfo=SHANGHAI)
        return "scheduled", "market_window", target

    @staticmethod
    def _request_hash(
        request: SendRequest,
        app: ApiAppContext,
        policy: CategoryPolicy,
    ) -> str:
        """规范化请求指纹，覆盖会改变真实短信副作用与稳定作用域的字段。"""

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
            "scheduled_at": request.scheduled_at.isoformat()
            if request.scheduled_at is not None
            else None,
            "consent_confirmed": request.consent_confirmed,
            "is_test": request.is_test,
            "mobiles": list(request.mobiles or ()),
            "protected_phone_masks": [
                item.phone_mask for item in request.protected_mobiles
            ],
            "vendor_test_uat": request.vendor_test_uat,
            "resend_of": request.resend_of,
            "resend_dept": request.resend_dept,
            "policy": {
                "queue": policy.queue,
                "blacklist_required": policy.blacklist_required,
            },
        }
        return hashlib.sha256(
            json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

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
    def _idempotency_app_id(
        request: SendRequest,
        app: ApiAppContext,
    ) -> int | None:
        """Web 人工发送没有真实 app 行，幂等作用域使用 NULL 并由协调层映射为 web。"""

        if request.channel == "web" and not request.vendor_test_uat:
            return None
        return app.app_id

    def _validate_schedule(self, request: SendRequest) -> None:
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
        app_id: int | None,
        biz_id: str,
        request_hash: str,
    ) -> None:
        """旧记录（无指纹）沿用原幂等行为；新记录指纹不一致时拒绝静默复用。"""

        stored_hash = await self.idempotency.request_hash(app_id, biz_id)
        if stored_hash is not None and stored_hash != request_hash:
            raise IdempotencyConflict(
                "同一幂等键已用于不同请求，请更换 biz_id 或复用原请求"
            )

    @staticmethod
    def _quota_clock(now: datetime) -> tuple[str, int]:
        local = now.astimezone(SHANGHAI)
        next_day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.strftime("%Y%m%d"), max(1, int((next_day - local).total_seconds()))

    async def preauthorize(
        self,
        app: ApiAppContext,
        category: str,
    ) -> AcceptancePreauthorization:
        """在受控号码解析前消费应用限流并验证类别权限。"""

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
        idem_app_id = self._idempotency_app_id(request, app)
        policy = self._resolve_policy(app, request, preauthorization)
        request_hash = self._request_hash(request, app, policy)
        existing = await self.idempotency.lookup(idem_app_id, biz_id)
        if existing is not None:
            await self._ensure_same_request(idem_app_id, biz_id, request_hash)
            return await self.store.response_for(existing)
        token = await self.idempotency.claim(idem_app_id, biz_id)
        if token is None:
            existing = await self.idempotency.wait(idem_app_id, biz_id)
            if existing is not None:
                await self._ensure_same_request(idem_app_id, biz_id, request_hash)
                return await self.store.response_for(existing)
            token = await self.idempotency.claim(idem_app_id, biz_id)
        if token is None:
            raise RuntimeError("idempotency coordination unavailable")
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self.idempotency.heartbeat(idem_app_id, biz_id, token, lost)
        )

        async def check_ownership() -> None:
            if lost.is_set():
                raise IdempotencyClaimLost("idempotency claim lost")
            try:
                owned = await self.idempotency.renew(idem_app_id, biz_id, token)
            except Exception:
                lost.set()
                raise IdempotencyClaimLost("idempotency claim unavailable") from None
            if not owned:
                lost.set()
                raise IdempotencyClaimLost("idempotency claim lost")

        try:
            existing = await self.idempotency.lookup(idem_app_id, biz_id)
            if existing is not None:
                await self._ensure_same_request(idem_app_id, biz_id, request_hash)
                return await self.store.response_for(existing)
            await check_ownership()
            return await self._accept_claimed(
                app,
                request,
                preauthorization=preauthorization,
                ownership_check=check_ownership,
                claim_key=self.idempotency.claim_key(idem_app_id, biz_id),
                claim_token=token,
                frequency_result_key=self.idempotency.frequency_result_key(
                    idem_app_id, biz_id
                ),
                idem_app_id=idem_app_id,
                request_hash=request_hash,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            try:
                await self.idempotency.release(idem_app_id, biz_id, token)
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
        idem_app_id: int | None = None,
        request_hash: str | None = None,
    ) -> BatchResponse:
        if ownership_check is not None:
            await ownership_check()
        idem_app_id = self._idempotency_app_id(request, app) if idem_app_id is None else idem_app_id
        if request.biz_id and request_hash is None:
            policy = self._resolve_policy(app, request, preauthorization)
            request_hash = self._request_hash(request, app, policy)
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
        if has_protected and not request.vendor_test_uat:
            raise ValueError("加密号码只能由真实联调 UAT 使用")
        if request.is_test and recipient_count > self.config.test_send_max:
            raise ValueError(f"测试发送最多{self.config.test_send_max}个号码")
        if self.recipient_guard is not None and has_plain:
            self.recipient_guard.require_allowed(request.mobiles)
        if preauthorization is None and self.acceptance_limiter is not None:
            await self.acceptance_limiter.check(
                app_id=app.app_id,
                limit_per_minute=app.rate_limit_per_min,
            )
        policy = self._resolve_policy(app, request, preauthorization)
        rendered = await self._render(request)
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
            ) = await run_bounded(
                self._protect_plain_phones,
                unique_phones,
                blacklist_required=policy.blacklist_required,
                timeout_s=10,
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
                (
                    f"acceptance:{app.app_id}:"
                    f"{hashlib.sha256(request.biz_id.encode()).hexdigest()}:{date_key}"
                )
                if request.biz_id
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
            if (
                self.usage_ledger is None
                or usage_reservation_id is None
                or usage_reservation_reused
            ):
                return
            try:
                await self.usage_ledger.request_release(
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
        if ownership_check is not None:
            await ownership_check()
        try:
            for item in after_blacklist:
                if self.usage_ledger is not None and usage_reservation_id is not None:
                    allowed = await self.usage_ledger.allow_frequency(
                        usage_reservation_id,
                        request.category,
                        app_id=app.app_id,
                        phone_hmac=frequency_hmac_by_active[item.phone_hmac],
                        hmac_aliases=frequency_aliases_by_active[item.phone_hmac],
                        limits=limits,
                        now=now,
                    )
                else:
                    allowed = await self.frequency.allow(
                        request.category,
                        app_id=app.app_id,
                        phone_hmac=frequency_hmac_by_active[item.phone_hmac],
                        limits=limits,
                        claim_key=claim_key,
                        claim_token=claim_token,
                        result_key=frequency_result_key,
                    )
                if allowed:
                    accepted.append(item)
        except Exception:
            await release_usage("acceptance-failed")
            raise
        removed_freq = len(after_blacklist) - len(accepted)
        if not accepted:
            await release_usage("all-filtered")
            raise AllFiltered("全部号码已被过滤")
        quota_cost = prepared.segments * len(accepted)
        if ownership_check is not None:
            await ownership_check()
        quota_reservation_key = (
            self.idempotency.quota_result_key(idem_app_id, request.biz_id, date_key)
            if request.biz_id
            else None
        )
        try:
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
                persisted_content=prepared.persisted_content,
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
                segments=prepared.segments,
                quota_cost=quota_cost,
                status=status,
                deferred_reason=deferred_reason,
                scheduled_at=scheduled_at,
                request_hash=request_hash,
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
                try:
                    existing = await self.idempotency.lookup(idem_app_id, request.biz_id)
                except Exception:
                    raise original from None
                if existing is not None:
                    await self._ensure_same_request(
                        idem_app_id, request.biz_id, request_hash or ""
                    )
                    await release_usage("idempotent-reuse")
                    return await self.store.response_for(existing)
            await compensate_reservation()
            raise
        if stored.idempotent:
            if request.biz_id:
                await self._ensure_same_request(
                    idem_app_id, request.biz_id, request_hash or ""
                )
            if self.usage_ledger is not None:
                await release_usage("idempotent-reuse")
            elif not reservation_reused:
                await compensate_reservation()
            return await self.store.response_for(stored.batch_no)
        if request.biz_id:
            await self.idempotency.remember(idem_app_id, request.biz_id, stored.batch_no)
        if status == "queued" and not stored.outbox_persisted:
            if ownership_check is not None:
                await ownership_check()
            await self.publisher.enqueue(stored.batch_no, policy.queue)
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
        )
