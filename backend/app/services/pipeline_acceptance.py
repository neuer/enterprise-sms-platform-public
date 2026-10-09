"""一次受理的分阶段实现：校验、内容准备、号码筛查、用量与频控、额度、落库与收尾。

各阶段共享 _Acceptance：调用方输入、在途/用量预留与绑定标志。释放与补偿只依据
这些标志决定；编排顺序见 SendPipeline._accept_claimed。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.core.apikey import ApiAppContext
from app.core.bounded_executor import run_bounded
from app.core.sensitive_text import reject_phone_in_text
from app.services.billing import calculate_segments
from app.services.category import CategoryPolicy, coerce_market_dispatch
from app.services.crypto import ProtectedPhone
from app.services.freq import FrequencyLimits
from app.services.idempotency import IdempotencyScope, usage_request_key
from app.services.masking import mask_phone_text, mask_verify_otp
from app.services.pipeline_contracts import (
    AcceptancePreauthorization,
    AllFiltered,
    ConsentRequired,
    FrequencyPort,
    InvalidContent,
    QueuePublisher,
    QuotaPort,
    RecipientGuard,
    SendRequest,
    SensitiveWord,
    SignPort,
    TemplatePort,
    UsageLedgerPort,
)
from app.services.pipeline_idempotency import AcceptanceIdempotencyMixin
from app.services.usage_ledger import FrequencyDecisionItem

# 与入口共用日志器名，日志路由与告警过滤保持不变。
LOGGER = logging.getLogger("app.services.pipeline")
SHANGHAI = ZoneInfo("Asia/Shanghai")
PHONE_NUMBER = re.compile(r"^1\d{10}$")


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


@dataclass(slots=True)
class _Acceptance:
    """一次受理在各阶段间共享的输入、预留与绑定状态。

    inflight_bound / allow_unbound_release 决定编排结束时是否释放未绑定的在途预留；
    usage_reservation_id / usage_reservation_reused 决定用量事实的释放。
    """

    app: ApiAppContext
    request: SendRequest
    preauthorization: AcceptancePreauthorization | None
    ownership_check: Callable[[], Awaitable[None]] | None
    claim_key: str | None
    claim_token: str | None
    claim_generation: int | None
    frequency_result_key: str | None
    idem_scope: IdempotencyScope | None
    request_hash: str | None
    request_hash_key_version: int | None
    recipient_count: int = 0
    has_plain: bool = False
    has_protected: bool = False
    inflight: Any = None
    inflight_bound: bool = False
    allow_unbound_release: bool = True
    usage_reservation_id: UUID | None = None
    usage_reservation_reused: bool = False

    async def check_ownership(self) -> None:
        if self.ownership_check is not None:
            await self.ownership_check()


@dataclass(frozen=True, slots=True)
class _ScreenedRecipients:
    """去重、加密与黑名单之后的号码；只含密文与不可逆索引。"""

    removed_duplicate: int
    removed_blacklist: int
    after_blacklist: list[ProtectedPhone]
    frequency_hmac_by_active: dict[str, str]
    frequency_aliases_by_active: dict[str, dict[int, str]]


@dataclass(frozen=True, slots=True)
class _UsageWindow:
    limits: FrequencyLimits
    now: datetime
    date_key: str
    ttl_s: int


@dataclass(frozen=True, slots=True)
class _QuotaHold:
    """已预扣的额度；未走用量账本时用于补偿退还。"""

    cost: int
    date_key: str
    reservation_key: str | None
    reused: bool


class AcceptanceStagesMixin(AcceptanceIdempotencyMixin):
    """SendPipeline 受理的各阶段；端口由 SendPipeline 注入，运行中替换立即生效。"""

    clock: Callable[[], datetime]
    templates: TemplatePort | None
    signs: SignPort | None
    recipient_guard: RecipientGuard | None
    frequency: FrequencyPort
    quota: QuotaPort
    publisher: QueuePublisher
    usage_ledger: UsageLedgerPort | None

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
            raise ValueError(f"scheduled_at 不能超过 {self.config.max_schedule_ahead_days} 天")

    async def _validate_acceptance(self, acceptance: _Acceptance) -> None:
        """入参、幂等指纹、号码来源与数量、白名单和请求级限流；不产生预留。"""

        app = acceptance.app
        request = acceptance.request
        preauthorization = acceptance.preauthorization
        reject_phone_in_text(request.remark, field_name="remark")
        self._validate_usage_subject(request)
        await acceptance.check_ownership()
        if acceptance.idem_scope is None and request.biz_id:
            acceptance.idem_scope = self._idempotency_scope(request, app)
        if request.biz_id and acceptance.idem_scope is None:
            raise RuntimeError("idempotency scope unavailable")
        if request.biz_id and acceptance.request_hash is None:
            policy = self._resolve_policy(app, request, preauthorization)
            acceptance.request_hash_key_version = self.crypto.active_version
            acceptance.request_hash = self._request_hash(
                request,
                app,
                policy,
                key_version=acceptance.request_hash_key_version,
            )
        if request.biz_id and acceptance.request_hash_key_version is None:
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
        if not request.biz_id and preauthorization is None:
            await self._authorize_new_send(request)
        if has_protected and not request.vendor_test_uat:
            raise ValueError("加密号码只能由真实联调 UAT 使用")
        if request.is_test and recipient_count > self.config.test_send_max:
            raise ValueError(f"测试发送最多{self.config.test_send_max}个号码")
        if self.recipient_guard is not None and has_plain:
            self.recipient_guard.require_allowed(request.mobiles)
        self._enforce_market_api_bulk(app, request, recipient_count)
        if (
            (request.channel != "web" or self._is_system_resend(request))
            and preauthorization is None
            and self.acceptance_limiter is not None
            and not request.biz_id
        ):
            await self.acceptance_limiter.check(
                app_id=app.app_id,
                limit_per_minute=app.rate_limit_per_min,
            )
        acceptance.has_plain = has_plain
        acceptance.has_protected = has_protected
        acceptance.recipient_count = recipient_count

    async def _prepare_acceptance(
        self, acceptance: _Acceptance
    ) -> tuple[CategoryPolicy, PreparedContent, str | None]:
        """类别策略、渲染与签名、最终内容计费，并按真实成本复核准入与额度豁免。"""

        app = acceptance.app
        request = acceptance.request
        recipient_count = acceptance.recipient_count
        policy = self._resolve_policy(app, request, acceptance.preauthorization)
        rendered = await self._render(request, self._usage_dept(app, request))
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
        batch_size = max(1, int(getattr(self.config, "vendor_batch_size", 500) or 500))
        await self._authorize_new_send(
            request,
            estimated_segments=max(1, prepared.segments * recipient_count),
            estimated_chunks=max(1, ceil(recipient_count / batch_size)),
        )
        self._enforce_quota_exemption(app, request)
        return policy, prepared, sign_name

    async def _release_inflight(self, acceptance: _Acceptance, reason: str) -> None:
        inflight = acceptance.inflight
        reservation_id = getattr(inflight, "id", None)
        generation = getattr(inflight, "generation", None)
        if reservation_id is None or generation is None:
            return
        narrow = getattr(self.store, "release_unbound_acceptance_reservation", None)
        wide = getattr(self.store, "release_in_flight_reservation", None)
        try:
            if reason == "acceptance-failed" and narrow is not None:
                await narrow(int(reservation_id), int(generation), acceptance.app.app_id)
                return
            if wide is None:
                return
            await wide(int(reservation_id), int(generation), reason)
        except Exception as exc:
            LOGGER.error(
                "in-flight reservation release unavailable",
                extra={
                    "app_id": acceptance.app.app_id,
                    "reservation_id": int(reservation_id),
                    "error_type": type(exc).__name__,
                },
            )

    async def _release_usage(self, acceptance: _Acceptance, reason: str) -> None:
        usage_reservation_id = acceptance.usage_reservation_id
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
                    "app_id": acceptance.app.app_id,
                    "reservation_id": str(usage_reservation_id),
                    "error_type": type(exc).__name__,
                },
            )

    async def _screen_recipients(
        self,
        acceptance: _Acceptance,
        policy: CategoryPolicy,
        prepared: PreparedContent,
    ) -> _ScreenedRecipients:
        """发送成本限流、敏感词、去重、号码保护与黑名单（含历史 HMAC 版本）。"""

        app = acceptance.app
        request = acceptance.request
        await self._consume_send_cost(
            app,
            request,
            recipient_count=acceptance.recipient_count,
            segment_count=prepared.segments * acceptance.recipient_count,
        )
        await acceptance.check_ownership()
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
        if acceptance.has_protected:
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
                or aliases.get(protected_source[0].key_version) != protected_source[0].phone_hmac
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
                ownership_check=acceptance.ownership_check,
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
        return _ScreenedRecipients(
            removed_duplicate=removed_duplicate,
            removed_blacklist=len(blocked_active),
            after_blacklist=after_blacklist,
            frequency_hmac_by_active=frequency_hmac_by_active,
            frequency_aliases_by_active=frequency_aliases_by_active,
        )

    async def _start_usage_reservation(self, acceptance: _Acceptance) -> _UsageWindow:
        """确定频控限额与上海自然日，并在用量账本中开启稳定预留。"""

        app = acceptance.app
        request = acceptance.request
        limits = FrequencyLimits.from_config(
            verify_per_minute=self.config.verify_per_minute,
            verify_per_day=self.config.verify_per_day,
            market_per_day=self.config.market_per_day,
            override=app.freq_override,
        )
        now = self.clock()
        date_key, ttl_s = self._quota_clock(now)
        if self.usage_ledger is not None:
            request_key = (
                usage_request_key(acceptance.idem_scope, request.biz_id, date_key)
                if request.biz_id and acceptance.idem_scope is not None
                else f"acceptance:{uuid4()}"
            )
            usage_reservation = await self.usage_ledger.start_reservation(
                request_key=request_key,
                app_id=self._usage_app_id(app, request),
                dept=self._usage_dept(app, request),
                category=request.category,
                now=now,
                subject_kind=self._usage_subject_kind(request),
            )
            acceptance.usage_reservation_id = UUID(str(usage_reservation.reservation_id))
            acceptance.usage_reservation_reused = bool(getattr(usage_reservation, "reused", False))
        return _UsageWindow(limits=limits, now=now, date_key=date_key, ttl_s=ttl_s)

    async def _apply_frequency(
        self,
        acceptance: _Acceptance,
        screened: _ScreenedRecipients,
        window: _UsageWindow,
    ) -> list[ProtectedPhone]:
        """号码频控；失败或全部被拒时请求释放本次用量事实。"""

        app = acceptance.app
        request = acceptance.request
        after_blacklist = screened.after_blacklist
        frequency_hmac_by_active = screened.frequency_hmac_by_active
        frequency_aliases_by_active = screened.frequency_aliases_by_active
        usage_reservation_id = acceptance.usage_reservation_id
        accepted: list[ProtectedPhone] = []
        try:
            await acceptance.check_ownership()
            if request.category == "notice" and self.usage_ledger is not None:
                # notice 无频控维度；保留前后副作用边界的 claim 校验。
                accepted.extend(after_blacklist)
            else:
                frequency_batch = 200
                for offset in range(0, len(after_blacklist), frequency_batch):
                    if offset > 0:
                        await acceptance.check_ownership()
                    batch = after_blacklist[offset : offset + frequency_batch]
                    if self.usage_ledger is not None and usage_reservation_id is not None:
                        decisions = await self.usage_ledger.allow_frequency_many(
                            usage_reservation_id,
                            request.category,
                            app_id=self._usage_app_id(app, request),
                            items=tuple(
                                FrequencyDecisionItem(
                                    phone_hmac=frequency_hmac_by_active[item.phone_hmac],
                                    hmac_aliases=frequency_aliases_by_active[item.phone_hmac],
                                )
                                for item in batch
                            ),
                            limits=window.limits,
                            now=window.now,
                        )
                    else:
                        decisions = []
                        for index, item in enumerate(batch):
                            if index > 0 and index % 25 == 0:
                                await acceptance.check_ownership()
                            decisions.append(
                                await self.frequency.allow(
                                    request.category,
                                    app_id=self._usage_app_id(app, request),
                                    phone_hmac=frequency_hmac_by_active[item.phone_hmac],
                                    limits=window.limits,
                                    claim_key=acceptance.claim_key,
                                    claim_token=acceptance.claim_token,
                                    result_key=acceptance.frequency_result_key,
                                )
                            )
                    accepted.extend(
                        item for item, allowed in zip(batch, decisions, strict=True) if allowed
                    )
        except Exception:
            await self._release_usage(acceptance, "acceptance-failed")
            raise
        if not accepted:
            await self._release_usage(acceptance, "all-filtered")
            raise AllFiltered("全部号码已被过滤")
        return accepted

    async def _reserve_quota(
        self,
        acceptance: _Acceptance,
        prepared: PreparedContent,
        accepted: Sequence[ProtectedPhone],
        window: _UsageWindow,
    ) -> _QuotaHold:
        """按最终计费条预扣应用与部门日额度；失败时请求释放本次用量事实。"""

        app = acceptance.app
        request = acceptance.request
        idem_scope = acceptance.idem_scope
        quota_cost = prepared.segments * len(accepted)
        if request.biz_id and idem_scope is not None:
            quota_reservation_key = self.idempotency.quota_result_key(
                idem_scope, request.biz_id, window.date_key
            )
        else:
            quota_reservation_key = None
        try:
            await acceptance.check_ownership()
            if self.usage_ledger is not None and acceptance.usage_reservation_id is not None:
                next_day = (window.now.astimezone(SHANGHAI) + timedelta(days=1)).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                await self.usage_ledger.reserve_quota(
                    acceptance.usage_reservation_id,
                    app_id=self._usage_app_id(app, request),
                    dept=self._usage_dept(app, request),
                    category=request.category,
                    date_key=window.date_key,
                    cost=quota_cost,
                    app_limit=app.daily_quota,
                    dept_limit=self.config.dept_daily_quota,
                    expires_at=next_day,
                )
                reservation_reused = acceptance.usage_reservation_reused
            else:
                reservation = await self.quota.reserve(
                    app_id=self._usage_app_id(app, request),
                    dept=self._usage_dept(app, request),
                    category=request.category,
                    date_key=window.date_key,
                    cost=quota_cost,
                    app_limit=app.daily_quota,
                    dept_limit=self.config.dept_daily_quota,
                    ttl_s=window.ttl_s,
                    claim_key=acceptance.claim_key,
                    claim_token=acceptance.claim_token,
                    reservation_key=quota_reservation_key,
                )
                reservation_reused = bool(getattr(reservation, "reused", False))
        except Exception:
            await self._release_usage(acceptance, "acceptance-failed")
            raise
        return _QuotaHold(
            cost=quota_cost,
            date_key=window.date_key,
            reservation_key=quota_reservation_key,
            reused=reservation_reused,
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

    async def _protect_plain_phones_batched(
        self,
        phones: Sequence[str],
        *,
        blacklist_required: bool,
        ownership_check: Callable[[], Awaitable[None]] | None = None,
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
            if ownership_check is not None and start > 0:
                await ownership_check()
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
