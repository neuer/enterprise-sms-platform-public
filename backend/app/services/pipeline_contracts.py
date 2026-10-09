"""发送受理流水线的错误类型、请求/响应数据与外部端口契约。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.core.auth.accounts import (
    ActorPrincipal,
)
from app.services.category import CategoryPolicy
from app.services.crypto import ProtectedPhone
from app.services.freq import FrequencyLimits
from app.services.idempotency import (
    IdempotencyFingerprint,
    IdempotencyScope,
)
from app.services.uncertain_source import UncertainSourceProof
from app.services.usage_subject import UsageSubject


class ConsentRequired(ValueError):
    """Web 营销未确认用户同意，对应 CONSENT_REQUIRED/422。"""


class InvalidContent(ValueError):
    """最终下发内容不满足长度约束。"""


class AllFiltered(ValueError):
    """号码经过去重/黑名单/频控后为空，对应 ALL_FILTERED/422。"""


class SensitiveWord(ValueError):
    """内容命中阻断敏感词，对应 SENSITIVE_WORD/422。"""


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


class AcceptCommitUnknown(RuntimeError):
    """COMMIT 结果无法确认，对应 DEPENDENCY_UNAVAILABLE/503。"""


class AcceptCommitConflict(RuntimeError):
    """reservation 已绑定不一致批次，对应 STATE_CONFLICT/409。"""


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
        estimated_segments: int | None = None,
        estimated_chunks: int | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FailedSourceReference:
    """失败重发的原消息快照，只携带稳定引用和不可逆号码索引。"""

    message_id: int
    created_at: datetime
    phone_hmac: str
    key_version: int


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
    failed_sources: tuple[FailedSourceReference, ...] = ()
    protected_mobiles: Sequence[ProtectedPhone] = ()
    protected_hmac_candidates: Sequence[tuple[int, str]] = ()
    vendor_test_uat: bool = False
    import_reservation_id: UUID | None = None
    usage_subject: UsageSubject | None = None
    uncertain_source_proof: UncertainSourceProof | None = field(default=None, repr=False)


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
    vendor_batch_size: int = 500


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
    failed_sources: tuple[FailedSourceReference, ...] = ()
    request_hash: str | None = None
    request_hash_key_version: int | None = None
    inflight_reservation_id: int | None = None
    inflight_reservation_generation: int | None = None
    idempotency_claim_token: str | None = None
    idempotency_claim_generation: int | None = None
    uncertain_source_proof: UncertainSourceProof | None = field(default=None, repr=False)


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

    async def reserve_in_flight_chunks(
        self,
        app_id: int,
        estimated: int,
        limit: int,
    ) -> object: ...

    async def release_in_flight_reservation(
        self,
        reservation_id: int,
        generation: int,
        reason: str,
    ) -> bool: ...

    async def release_unbound_acceptance_reservation(
        self,
        reservation_id: int,
        generation: int,
        app_id: int,
    ) -> bool: ...

    async def resolve_ambiguous_acceptance_commit(
        self,
        *,
        reservation_id: int,
        generation: int,
        app_id: int,
        scope_kind: str,
        scope_id: str,
        biz_id: str,
        request_hash: str,
    ) -> object: ...


class IdempotencyPort(Protocol):
    def claim_key(self, scope: IdempotencyScope, biz_id: str) -> str: ...

    def frequency_result_key(self, scope: IdempotencyScope, biz_id: str) -> str: ...

    def quota_result_key(self, scope: IdempotencyScope, biz_id: str, date_key: str) -> str: ...

    async def request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyFingerprint | None: ...

    async def lookup(self, scope: IdempotencyScope, biz_id: str) -> str | None: ...

    async def remember(self, scope: IdempotencyScope, biz_id: str, batch_no: str) -> None: ...

    async def claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        fingerprint: str = "",
    ) -> str | None: ...

    async def wait(self, scope: IdempotencyScope, biz_id: str) -> str | None: ...

    async def release(self, scope: IdempotencyScope, biz_id: str, token: str) -> None: ...

    async def renew(self, scope: IdempotencyScope, biz_id: str, token: str) -> bool: ...

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
        subject_kind: str = "api_app",
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
