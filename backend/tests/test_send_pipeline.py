from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import SecurityPrincipal
from app.services.category import CategoryNotAllowed, policy_for_category
from app.services.crypto import CryptoService, EncryptionContext, ProtectedPhone
from app.services.freq import FrequencyFenceLost
from app.services.idempotency import (
    CLAIM_RELEASE_LUA,
    CLAIM_RENEW_LUA,
    IdempotencyCoordinator,
)
from app.services.pipeline import (
    AcceptancePreauthorization,
    AllFiltered,
    BatchResponse,
    ConsentRequired,
    IdempotencyConflict,
    PipelineConfig,
    SendPipeline,
    SendRequest,
    StoredBatch,
    VendorTestConsoleOnly,
)
from app.services.quota import QuotaFenceLost
from app.services.vendor_test_guard import VendorTestRecipientDenied

ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")
OPERATOR = SecurityPrincipal(2, 20, "operator01", "平台部", "operator")


def crypto() -> CryptoService:
    key = base64.b64encode(b"x" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def rotated_crypto() -> CryptoService:
    first = base64.b64encode(b"x" * 32).decode()
    second = base64.b64encode(b"y" * 32).decode()
    ring = json.dumps({"active_version": 2, "keys": {"1": first, "2": second}})
    return CryptoService.from_secret_values(ring, ring)


class FakeIdempotency:
    def __init__(
        self,
        existing: str | None = None,
        *,
        stored_request_hash: str | None = None,
    ) -> None:
        self.existing = existing
        self.stored_request_hash = stored_request_hash
        self.remembered: list[tuple[int, str, str]] = []
        self.released: list[str] = []
        self.lookup_calls: list[tuple[int | None, str]] = []

    async def lookup(self, app_id: int | None, biz_id: str) -> str | None:
        self.lookup_calls.append((app_id, biz_id))
        return self.existing

    async def request_hash(self, app_id: int | None, biz_id: str) -> str | None:
        return self.stored_request_hash

    def claim_key(self, app_id: int | None, biz_id: str) -> str:
        return f"idem:claim:{app_id}:{biz_id}"

    def frequency_result_key(self, app_id: int | None, biz_id: str) -> str:
        return f"idem:freq:{app_id}:{biz_id}"

    def quota_result_key(self, app_id: int | None, biz_id: str, date_key: str) -> str:
        return f"idem:quota:{app_id}:{biz_id}:{date_key}"

    async def remember(self, app_id: int | None, biz_id: str, batch_no: str) -> None:
        self.remembered.append((app_id, biz_id, batch_no))

    async def claim(self, app_id: int | None, biz_id: str) -> str | None:
        return "claim-token"

    async def wait(self, app_id: int | None, biz_id: str) -> str | None:
        return self.existing

    async def release(self, app_id: int | None, biz_id: str, token: str) -> None:
        self.released.append(token)

    async def renew(self, app_id: int | None, biz_id: str, token: str) -> bool:
        return True

    async def heartbeat(
        self,
        app_id: int | None,
        biz_id: str,
        token: str,
        lost: asyncio.Event,
    ) -> None:
        await asyncio.Event().wait()


class FakeStore:
    def __init__(self) -> None:
        self.commands: list[Any] = []
        self.sensitive_hits_result: list[str] = []
        self.sensitive_audits: list[tuple[int, int]] = []
        self.response = BatchResponse(
            "existing",
            True,
            1,
            0,
            0,
            0,
            1,
            1,
            "queued",
            None,
            None,
        )

    async def response_for(self, batch_no: str) -> BatchResponse:
        return self.response

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        return set()

    async def sensitive_hits(self, content: str) -> list[str]:
        return self.sensitive_hits_result

    async def audit_sensitive_hit(self, app_id: int, hit_count: int) -> None:
        self.sensitive_audits.append((app_id, hit_count))

    async def save(self, command: Any) -> StoredBatch:
        self.commands.append(command)
        return StoredBatch("new-batch", False)


class FailingStore(FakeStore):
    async def save(self, command: Any) -> StoredBatch:
        raise RuntimeError("database unavailable")


class IdempotentStore(FakeStore):
    async def save(self, command: Any) -> StoredBatch:
        self.commands.append(command)
        return StoredBatch("existing", True)


class BlockAllStore(FakeStore):
    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        return phone_hmacs


class HistoricalBlacklistStore(FakeStore):
    def __init__(self, blocked: str) -> None:
        super().__init__()
        self.blocked = blocked
        self.seen: set[str] = set()

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        self.seen = phone_hmacs
        return {self.blocked} & phone_hmacs


class FakeFrequency:
    def __init__(self, rejected_suffix: str = "") -> None:
        self.rejected_suffix = rejected_suffix
        self.calls = 0
        self.values: list[dict[str, Any]] = []

    async def allow(self, category: str, **values: Any) -> bool:
        self.calls += 1
        self.values.append(values)
        if not self.rejected_suffix:
            return True
        return not values["phone_hmac"].endswith(self.rejected_suffix)


class FakeQuota:
    def __init__(self) -> None:
        self.reservations: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []

    async def reserve(self, **values: Any) -> None:
        self.reservations.append(values)

    async def refund(self, **values: Any) -> None:
        self.refunds.append(values)

    async def refund_reservation(self, **values: Any) -> None:
        self.refunds.append(values)


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def enqueue(self, batch_no: str, queue: str) -> None:
        self.events.append((batch_no, queue))


class FakeUsageLedger:
    def __init__(self, *, allowed: bool = True, reused: bool = False) -> None:
        self.reservation_id = uuid4()
        self.allowed = allowed
        self.reused = reused
        self.started: list[dict[str, Any]] = []
        self.frequency: list[dict[str, Any]] = []
        self.quota: list[dict[str, Any]] = []
        self.releases: list[tuple[UUID, str]] = []

    async def start_reservation(self, **values: Any) -> object:
        self.started.append(values)
        return SimpleNamespace(reservation_id=self.reservation_id, reused=self.reused)

    async def allow_frequency(
        self,
        reservation_id: UUID,
        category: str,
        **values: Any,
    ) -> bool:
        self.frequency.append({"reservation_id": reservation_id, "category": category, **values})
        return self.allowed

    async def reserve_quota(self, reservation_id: UUID, **values: Any) -> None:
        self.quota.append({"reservation_id": reservation_id, **values})

    async def request_release(self, reservation_id: UUID, *, event_id: str) -> bool:
        self.releases.append((reservation_id, event_id))
        return True


@pytest.mark.asyncio
async def test_usage_ledger_replaces_redis_counters_and_commits_stable_reference() -> None:
    ledger = FakeUsageLedger()
    frequency = FakeFrequency()
    quota = FakeQuota()
    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=rotated_crypto(),
        frequency=frequency,
        quota=quota,
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
        clock=lambda: datetime(2026, 7, 26, 8, 30, tzinfo=UTC),
    )

    response = await pipeline.accept(
        ApiAppContext(7, "app", "研发部", frozenset({"verify"}), daily_quota=10),
        SendRequest("verify", ["13800138000"], content="验证码123456"),
    )

    assert response.batch_no == "new-batch"
    assert frequency.calls == 0
    assert quota.reservations == []
    assert ledger.frequency[0]["hmac_aliases"].keys() == {1, 2}
    assert ledger.quota[0]["cost"] == 1
    assert store.commands[0].usage_reservation_id == ledger.reservation_id
    assert ledger.releases == []


@pytest.mark.asyncio
async def test_database_save_failure_creates_durable_usage_release_request() -> None:
    ledger = FakeUsageLedger()
    pipeline = SendPipeline(
        store=FailingStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="orphan-retry",
            ),
        )

    assert ledger.releases == [
        (
            ledger.reservation_id,
            f"usage:{ledger.reservation_id}:acceptance-failed",
        )
    ]


@pytest.mark.asyncio
async def test_database_idempotent_reuse_releases_new_usage_facts_once() -> None:
    ledger = FakeUsageLedger()
    store = IdempotentStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )

    result = await pipeline.accept(
        ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="通知",
            biz_id="idempotent-race",
        ),
    )

    assert result.idempotent is True
    assert ledger.releases == [
        (
            ledger.reservation_id,
            f"usage:{ledger.reservation_id}:idempotent-reuse",
        )
    ]


@pytest.mark.asyncio
async def test_database_idempotent_reuse_keeps_original_usage_facts() -> None:
    ledger = FakeUsageLedger(reused=True)
    pipeline = SendPipeline(
        store=IdempotentStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )

    result = await pipeline.accept(
        ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="通知",
            biz_id="idempotent-race",
        ),
    )

    assert result.idempotent is True
    assert ledger.releases == []


@pytest.mark.asyncio
async def test_reused_usage_reservation_failure_is_left_for_owner_or_recovery() -> None:
    ledger = FakeUsageLedger(reused=True)
    pipeline = SendPipeline(
        store=FailingStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="orphan-retry",
            ),
        )

    assert ledger.releases == []


@pytest.mark.asyncio
async def test_frequency_filtered_request_releases_all_partial_usage_facts() -> None:
    ledger = FakeUsageLedger(allowed=False)
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )

    with pytest.raises(AllFiltered):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"verify"})),
            SendRequest("verify", ["13800138000"], content="验证码123456"),
        )

    assert ledger.quota == []
    assert ledger.releases[0][1].endswith(":all-filtered")


@pytest.mark.asyncio
async def test_controlled_api_preauthorization_limits_once_and_is_reused_by_accept() -> None:
    class RecordingLimiter:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def check(self, *, app_id: int, limit_per_minute: int) -> None:
            self.calls.append((app_id, limit_per_minute))

    limiter = RecordingLimiter()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=limiter,  # type: ignore[arg-type]
        vendor_test_console_only=True,
    )
    app = ApiAppContext(
        7,
        "uat-app",
        "平台部",
        frozenset({"notice"}),
        rate_limit_per_min=9,
    )

    authorization = await pipeline.preauthorize(app, "notice")
    assert isinstance(authorization, AcceptancePreauthorization)
    service_crypto = crypto()
    protected = service_crypto.protect_phone(
        "13800138000",
        table="vendor_test_recipient",
    )
    await pipeline.accept(
        app,
        SendRequest(
            category="notice",
            mobiles=(),
            content="维护通知",
            biz_id="api-uat-preauth",
            is_test=True,
            protected_mobiles=(protected,),
            protected_hmac_candidates=tuple(
                service_crypto.hmac_candidates("13800138000").items()
            ),
            vendor_test_uat=True,
        ),
        preauthorization=authorization,
    )

    assert limiter.calls == [(7, 9)]


@pytest.mark.asyncio
async def test_controlled_api_preauthorization_limits_before_category_denial() -> None:
    class RecordingLimiter:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def check(self, *, app_id: int, limit_per_minute: int) -> None:
            self.calls.append((app_id, limit_per_minute))

    limiter = RecordingLimiter()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=limiter,  # type: ignore[arg-type]
    )
    app = ApiAppContext(
        7,
        "uat-app",
        "平台部",
        frozenset({"verify"}),
        rate_limit_per_min=9,
    )

    with pytest.raises(CategoryNotAllowed):
        await pipeline.preauthorize(app, "notice")

    assert limiter.calls == [(7, 9)]


@pytest.mark.asyncio
async def test_console_uat_reprotects_with_current_key_and_all_blacklist_candidates() -> None:
    current_crypto = rotated_crypto()
    historical_hmac = current_crypto.phone_hmac("13900000001", 1)
    store = HistoricalBlacklistStore(historical_hmac)
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=current_crypto,
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        vendor_test_console_only=True,
    )
    with pytest.raises(AllFiltered):
        await pipeline.accept(
            ApiAppContext(7, "uat-app", "平台部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ("13900000001",),
                content="维护通知",
                channel="web",
                actor=ADMIN,
                is_test=True,
                vendor_test_uat=True,
            ),
        )

    assert store.seen == set(current_crypto.hmac_candidates("13900000001").values())


@pytest.mark.asyncio
async def test_live_mode_blocks_plain_send_and_uat_market_still_requires_consent() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        vendor_test_console_only=True,
    )
    app = ApiAppContext(7, "uat-app", "平台部", frozenset({"notice", "market"}))

    with pytest.raises(VendorTestConsoleOnly):
        await pipeline.accept(
            app,
            SendRequest("notice", ("13800138000",), content="维护通知"),
        )
    with pytest.raises(ConsentRequired):
        await pipeline.accept(
            app,
            SendRequest(
                "market",
                (),
                content="活动",
                channel="web",
                is_test=True,
                protected_mobiles=(ProtectedPhone(b"cipher", "a" * 64, "mask", 1),),
                protected_hmac_candidates=((1, "a" * 64),),
                vendor_test_uat=True,
            ),
        )


@pytest.mark.asyncio
async def test_live_test_guard_rejects_before_crypto_quota_storage_or_publish() -> None:
    class RejectingGuard:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def require_allowed(self, phones: list[str]) -> None:
            self.calls.append(tuple(phones))
            raise VendorTestRecipientDenied(1)

    class UnexpectedCrypto:
        def protect_phone(self, _phone: str) -> None:
            raise AssertionError("白名单拒绝后不得保护并持久化手机号")

    class UnexpectedLimiter:
        async def check(self, **_values: object) -> None:
            raise AssertionError("白名单拒绝后不得写入应用限流状态")

    guard = RejectingGuard()
    store = FakeStore()
    quota = FakeQuota()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=UnexpectedCrypto(),  # type: ignore[arg-type]
        frequency=FakeFrequency(),
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
        recipient_guard=guard,
        acceptance_limiter=UnexpectedLimiter(),  # type: ignore[arg-type]
    )

    with pytest.raises(VendorTestRecipientDenied) as captured:
        await pipeline.accept(
            ApiAppContext(7, "app", "平台部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000", "13900139000"],
                content="维护通知",
            ),
        )

    assert captured.value.denied_count == 1
    assert guard.calls == [("13800138000", "13900139000")]
    assert store.commands == []
    assert quota.reservations == []
    assert publisher.events == []


@pytest.mark.asyncio
async def test_idempotency_hit_does_not_recheck_or_resend_live_test_recipient() -> None:
    class UnexpectedGuard:
        def require_allowed(self, _phones: list[str]) -> None:
            raise AssertionError("幂等命中只读取原批次，不重走真实发送链")

    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency("existing"),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        recipient_guard=UnexpectedGuard(),
    )

    result = await pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="维护通知",
            biz_id="existing-request",
        ),
    )

    assert result.idempotent is True
    assert store.commands == []


@pytest.mark.asyncio
async def test_idempotency_short_circuits_frequency_quota_and_storage() -> None:
    store = FakeStore()
    frequency = FakeFrequency()
    quota = FakeQuota()

    class UnexpectedLimiter:
        async def check(self, **_values: object) -> None:
            raise AssertionError("幂等命中不得消耗应用限流")

    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency("existing"),
        crypto=crypto(),
        frequency=frequency,
        quota=quota,
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=UnexpectedLimiter(),  # type: ignore[arg-type]
    )
    result = await pipeline.accept(
        ApiAppContext(1, "app", "研发部", frozenset({"verify"})),
        SendRequest("verify", ["13800138000"], content="验证码123456", biz_id="biz-1"),
    )
    assert result.idempotent is True
    assert frequency.calls == 0
    assert quota.reservations == []
    assert store.commands == []


@pytest.mark.asyncio
async def test_idempotency_same_key_different_hash_conflicts() -> None:
    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency("existing", stored_request_hash="other-hash"),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    with pytest.raises(IdempotencyConflict, match="不同请求"):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="biz-1",
            ),
        )
    assert store.commands == []


@pytest.mark.asyncio
async def test_web_idempotency_uses_web_scope_and_same_hash_returns_original() -> None:
    app = ApiAppContext(0, "web", "平台部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="维护通知",
        biz_id="web-biz-1",
        channel="web",
        actor=ADMIN,
    )
    policy = policy_for_category(
        request.category,
        app.allowed_categories,
        notice_blacklist=app.blacklist_check,
    )
    expected_hash = SendPipeline._request_hash(request, app, policy)
    idempotency = FakeIdempotency("existing", stored_request_hash=expected_hash)
    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    result = await pipeline.accept(app, request)

    assert result.idempotent is True
    assert idempotency.lookup_calls == [(None, "web-biz-1")]
    assert store.commands == []


@pytest.mark.asyncio
async def test_initial_accept_rejects_past_or_beyond_max_schedule_window() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(max_schedule_ahead_days=90),
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
    )
    app = ApiAppContext(1, "app", "研发部", frozenset({"notice"}))

    with pytest.raises(ValueError, match="future"):
        await pipeline.accept(
            app,
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                scheduled_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
                biz_id="past-schedule",
            ),
        )
    with pytest.raises(ValueError, match="不能超过"):
        await pipeline.accept(
            app,
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                scheduled_at=datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
                biz_id="far-schedule",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.concurrency
@pytest.mark.idempotency
async def test_concurrent_idempotent_requests_execute_side_effects_once() -> None:
    class ClaimRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.expires_at: dict[str, float] = {}
            self.now = 0.0
            self.renewals = 0

        def _expire(self, key: str) -> None:
            if self.expires_at.get(key, float("inf")) <= self.now:
                self.values.pop(key, None)
                self.expires_at.pop(key, None)

        async def get(self, key: str) -> str | None:
            self._expire(key)
            return self.values.get(key)

        async def set(self, key: str, value: str, **kwargs: Any) -> bool:
            if kwargs.get("nx") and key in self.values:
                return False
            self.values[key] = value
            if "ex" in kwargs:
                self.expires_at[key] = self.now + float(kwargs["ex"])
            return True

        async def delete(self, key: str) -> None:
            self.values.pop(key, None)

        async def eval(self, script: str, _keys: int, key: str, token: str, *args: Any) -> int:
            self._expire(key)
            if script == CLAIM_RENEW_LUA:
                if self.values.get(key) != token:
                    return 0
                self.renewals += 1
                self.expires_at[key] = self.now + float(args[0])
                return 1
            assert script == CLAIM_RELEASE_LUA
            if self.values.get(key) != token:
                return 0
            self.values.pop(key, None)
            self.expires_at.pop(key, None)
            return 1

    class ConcurrentStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.by_biz: dict[tuple[int, str], tuple[str, str]] = {}

        async def exists(self, app_id: int, biz_id: str, batch_no: str) -> bool:
            return self.by_biz.get((app_id, biz_id), (None, None))[0] == batch_no

        async def find_existing(self, app_id: int, biz_id: str) -> str | None:
            return self.by_biz.get((app_id, biz_id), (None, None))[0]

        async def find_request_hash(
            self, app_id: int | None, biz_id: str
        ) -> str | None:
            return self.by_biz.get((app_id, biz_id), (None, None))[1]

        async def save(self, command: Any) -> StoredBatch:
            self.commands.append(command)
            self.by_biz[(int(command.app_id), str(command.biz_id))] = (
                "shared-batch",
                command.request_hash,
            )
            return StoredBatch("shared-batch", False)

        async def response_for(self, batch_no: str) -> BatchResponse:
            return BatchResponse(
                batch_no, True, 1, 0, 0, 0, 1, 1, "queued", None, None
            )

    class BlockingFrequency(FakeFrequency):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.resume = asyncio.Event()

        async def allow(self, category: str, **values: Any) -> bool:
            self.calls += 1
            self.entered.set()
            await self.resume.wait()
            return True

    redis = ClaimRedis()
    store = ConcurrentStore()
    frequency = BlockingFrequency()
    quota = FakeQuota()
    publisher = FakePublisher()

    async def advance(seconds: float) -> None:
        redis.now += seconds
        await asyncio.sleep(0)

    coordinator = IdempotencyCoordinator(
        redis,
        store,
        claim_ttl_s=6,
        heartbeat_interval_s=2,
        wait_attempts=30,
        wait_interval_s=1,
        sleeper=advance,
    )
    pipeline = SendPipeline(
        store=store,
        idempotency=coordinator,
        crypto=crypto(),
        frequency=frequency,
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
    )
    request = SendRequest(
        "verify",
        ["13800138000"],
        content="验证码123456",
        biz_id="same-biz",
    )

    owner = asyncio.create_task(
        pipeline.accept(ApiAppContext(7, "app", "研发部", frozenset({"verify"})), request)
    )
    await frequency.entered.wait()
    contender = asyncio.create_task(
        pipeline.accept(ApiAppContext(7, "app", "研发部", frozenset({"verify"})), request)
    )
    for _attempt in range(30):
        if redis.renewals >= 3 and redis.now > 6:
            break
        await asyncio.sleep(0)
    assert redis.renewals >= 3
    assert redis.now > 6
    frequency.resume.set()
    first, second = await asyncio.gather(owner, contender)

    assert first.batch_no == second.batch_no == "shared-batch"
    assert sorted([first.idempotent, second.idempotent]) == [False, True]
    assert frequency.calls == 1
    assert len(quota.reservations) == 1
    assert len(store.commands) == 1
    assert publisher.events == [("shared-batch", "realtime")]


@pytest.mark.asyncio
async def test_idempotency_claim_is_released_after_pipeline_failure() -> None:
    class ClaimRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, **kwargs: Any) -> bool:
            if kwargs.get("nx") and key in self.values:
                return False
            self.values[key] = value
            return True

        async def delete(self, key: str) -> None:
            self.values.pop(key, None)

        async def eval(
            self,
            _script: str,
            _keys: int,
            key: str,
            token: str,
            *_args: Any,
        ) -> int:
            if _script == CLAIM_RENEW_LUA:
                return int(self.values.get(key) == token)
            assert _script == CLAIM_RELEASE_LUA
            if self.values.get(key) != token:
                return 0
            self.values.pop(key, None)
            return 1

    class RepositoryFailingStore(FailingStore):
        async def exists(self, app_id: int, biz_id: str, batch_no: str) -> bool:
            return False

        async def find_existing(self, app_id: int, biz_id: str) -> str | None:
            return None

    redis = ClaimRedis()
    coordinator = IdempotencyCoordinator(redis, RepositoryFailingStore())
    pipeline = SendPipeline(
        store=RepositoryFailingStore(),
        idempotency=coordinator,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="retryable-biz",
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            request,
        )
    assert not any(key.startswith("idem:claim:") for key in redis.values)
    assert await coordinator.claim(7, "retryable-biz") is not None


@pytest.mark.asyncio
async def test_lost_claim_fails_closed_before_business_side_effects() -> None:
    class LostClaimIdempotency(FakeIdempotency):
        async def renew(self, app_id: int | None, biz_id: str, token: str) -> bool:
            return False

    store = FakeStore()
    frequency = FakeFrequency()
    quota = FakeQuota()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=LostClaimIdempotency(),
        crypto=crypto(),
        frequency=frequency,
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
    )

    with pytest.raises(RuntimeError, match="claim lost"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="lost-claim",
            ),
        )

    assert frequency.calls == 0
    assert quota.reservations == []
    assert store.commands == []
    assert publisher.events == []


@pytest.mark.asyncio
async def test_replaced_claim_stops_frequency_loop_before_quota_and_storage() -> None:
    ownership = {"token": "claim-token"}

    class ReplacingFrequency(FakeFrequency):
        def __init__(self) -> None:
            super().__init__()
            self.writes = 0

        async def allow(self, category: str, **values: Any) -> bool:
            assert values["claim_key"] == "idem:claim:7:freq-race"
            assert values["result_key"] == "idem:freq:7:freq-race"
            if ownership["token"] != values["claim_token"]:
                raise FrequencyFenceLost("frequency fence lost")
            self.writes += 1
            ownership["token"] = "new-owner"
            return True

    store = FakeStore()
    frequency = ReplacingFrequency()
    quota = FakeQuota()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=frequency,
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
    )

    with pytest.raises(FrequencyFenceLost):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"verify"})),
            SendRequest(
                "verify",
                ["13800138000", "13900139000"],
                content="验证码123456",
                biz_id="freq-race",
            ),
        )

    assert frequency.writes == 1
    assert quota.reservations == []
    assert store.commands == []
    assert publisher.events == []


@pytest.mark.asyncio
async def test_quota_lua_fence_closes_check_to_increment_race() -> None:
    ownership = {"token": "claim-token"}

    class RacingQuota(FakeQuota):
        async def reserve(self, **values: Any) -> None:
            ownership["token"] = "new-owner"
            if ownership["token"] != values["claim_token"]:
                raise QuotaFenceLost("quota fence lost")
            self.reservations.append(values)

    store = FakeStore()
    quota = RacingQuota()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
    )

    with pytest.raises(QuotaFenceLost):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="quota-race"),
        )

    assert quota.reservations == []
    assert store.commands == []
    assert publisher.events == []


@pytest.mark.asyncio
async def test_claim_loss_after_reserve_refunds_without_save_or_publish() -> None:
    class LosingIdempotency(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.lost = False

        async def renew(self, app_id: int | None, biz_id: str, token: str) -> bool:
            return not self.lost

    idem = LosingIdempotency()

    class LosingQuota(FakeQuota):
        async def reserve(self, **values: Any) -> None:
            self.reservations.append(values)
            idem.lost = True

    store = FakeStore()
    quota = LosingQuota()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=idem,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
    )

    with pytest.raises(RuntimeError, match="claim lost"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="refund-race"),
        )

    assert len(quota.refunds) == 1
    assert store.commands == []
    assert publisher.events == []


@pytest.mark.asyncio
async def test_release_failure_does_not_mask_successful_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ReleaseFailureIdempotency(FakeIdempotency):
        async def release(self, app_id: int | None, biz_id: str, token: str) -> None:
            raise RuntimeError("redis unavailable")

    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=ReleaseFailureIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    result = await pipeline.accept(
        ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="通知",
            biz_id="release-failure",
        ),
    )

    assert result.batch_no == "new-batch"
    assert "idempotency claim release unavailable" in caplog.text
    assert "release-failure" not in caplog.text
    assert "claim-token" not in caplog.text


@pytest.mark.asyncio
async def test_verify_pipeline_deduplicates_encrypts_masks_and_enqueues_reference_only() -> None:
    service = crypto()
    store = FakeStore()
    quota = FakeQuota()
    publisher = FakePublisher()
    app = ApiAppContext(
        7,
        "app-iam",
        "平台部",
        frozenset({"verify"}),
        "【青鸾】",
        100,
    )
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=service,
        frequency=FakeFrequency(),
        quota=quota,
        publisher=publisher,
        config=PipelineConfig(),
    )
    result = await pipeline.accept(
        app,
        SendRequest(
            "verify",
            ["13800138000", "13800138000", "13900139000"],
            content="验证码123456",
            biz_id="biz-2",
            resend_of="original-batch",
        ),
    )

    command = store.commands[0]
    assert result.accepted == 2
    assert result.removed_duplicate == 1
    assert command.persisted_content == "验证码******"
    assert "123456" not in command.persisted_content
    assert service.decrypt_bound_packed_text(
        command.send_content_enc,
        EncryptionContext(
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
            object_id=command.batch_no,
        ),
    ) == "验证码123456"
    assert command.sign_name == "【青鸾】"
    assert command.resend_of == "original-batch"
    assert all(message.phone_mask in {"138****8000", "139****9000"} for message in command.messages)
    assert all(not hasattr(message, "phone") for message in command.messages)
    assert quota.reservations[0]["cost"] == 2
    assert quota.reservations[0]["category"] == "verify"
    assert publisher.events == [("new-batch", "realtime")]


@pytest.mark.asyncio
async def test_pipeline_resolves_approved_default_sign_before_billing_and_storage() -> None:
    class FakeSigns:
        async def resolve(self, name: str) -> str:
            assert name == "青鸾平台"
            return "【青鸾平台】"

    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        signs=FakeSigns(),
        config=PipelineConfig(),
    )
    await pipeline.accept(
        ApiAppContext(
            7,
            "app-oa",
            "平台部",
            frozenset({"notice"}),
            default_sign="青鸾平台",
        ),
        SendRequest("notice", ["13800138000"], content="通知"),
    )
    assert store.commands[0].sign_name == "【青鸾平台】"
    assert store.commands[0].segments == 1


@pytest.mark.asyncio
async def test_sensitive_audit_policy_records_count_and_allows_send() -> None:
    store = FakeStore()
    store.sensitive_hits_result = ["敏感", "禁词"]
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(sensitive_hit_action="audit"),
    )
    await pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"verify"})),
        SendRequest("verify", ["13800138000"], content="敏感内容"),
    )
    assert store.sensitive_audits == [(7, 2)]


@pytest.mark.asyncio
async def test_all_filtered_reserves_no_quota_so_no_refund_is_needed() -> None:
    quota = FakeQuota()
    pipeline = SendPipeline(
        store=BlockAllStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=quota,
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(AllFiltered):
        await pipeline.accept(
            ApiAppContext(7, "app", "平台部", frozenset({"market"})),
            SendRequest("market", ["13800138000"], content="营销回T退订"),
        )
    assert quota.reservations == []
    assert quota.refunds == []


@pytest.mark.asyncio
async def test_pipeline_historical_blacklist_filters_active_protected_phone() -> None:
    service_crypto = rotated_crypto()
    blocked_candidates = service_crypto.hmac_candidates("13800138000")
    store = HistoricalBlacklistStore(blocked_candidates[1])
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=service_crypto,
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    result = await pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"market"})),
        SendRequest(
            "market",
            ["13800138000", "13900139000"],
            content="营销回T退订",
        ),
    )

    assert result.removed_blacklist == 1
    assert blocked_candidates[1] in store.seen
    assert blocked_candidates[2] in store.seen
    assert len(store.commands[0].messages) == 1
    assert all(
        message.phone_hmac != blocked_candidates[1] for message in store.commands[0].messages
    )


@pytest.mark.asyncio
async def test_protected_uat_checks_all_hmac_aliases_and_uses_stable_frequency_key() -> None:
    service_crypto = rotated_crypto()
    candidates = service_crypto.hmac_candidates("13800138000")
    protected = service_crypto.protect_phone(
        "13800138000",
        table="vendor_test_recipient",
    )
    store = HistoricalBlacklistStore(candidates[1])
    frequency = FakeFrequency()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=service_crypto,
        frequency=frequency,
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    with pytest.raises(AllFiltered):
        await pipeline.accept(
            ApiAppContext(7, "app", "平台部", frozenset({"notice"})),
            SendRequest(
                "notice",
                (),
                content="维护通知",
                protected_mobiles=(protected,),
                protected_hmac_candidates=tuple(sorted(candidates.items())),
                vendor_test_uat=True,
            ),
        )

    assert store.seen == set(candidates.values())
    assert frequency.calls == 0

    clean_store = FakeStore()
    clean_frequency = FakeFrequency()
    clean_pipeline = SendPipeline(
        store=clean_store,
        idempotency=FakeIdempotency(),
        crypto=service_crypto,
        frequency=clean_frequency,
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    await clean_pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"notice"})),
        SendRequest(
            "notice",
            (),
            content="维护通知",
            protected_mobiles=(protected,),
            protected_hmac_candidates=tuple(sorted(candidates.items())),
            vendor_test_uat=True,
        ),
    )
    assert clean_frequency.values[0]["phone_hmac"] == candidates[1]


@pytest.mark.asyncio
async def test_plain_send_uses_same_oldest_retained_hmac_for_frequency() -> None:
    service_crypto = rotated_crypto()
    frequency = FakeFrequency()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=service_crypto,
        frequency=frequency,
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    await pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"notice"})),
        SendRequest("notice", ["13800138000"], content="维护通知"),
    )

    assert frequency.values[0]["phone_hmac"] == service_crypto.hmac_candidates("13800138000")[1]


@pytest.mark.asyncio
async def test_web_market_uses_independent_threshold_and_waits_for_approval() -> None:
    store = FakeStore()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=publisher,
        config=PipelineConfig(approval_threshold=100, market_approval_threshold=2),
    )
    result = await pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"market"})),
        SendRequest(
            "market",
            ["13800138000", "13900139000"],
            content="活动回T退订",
            channel="web",
            consent_confirmed=True,
            actor=OPERATOR,
        ),
    )
    assert result.status == "pending_approval"
    assert store.commands[0].principal == OPERATOR
    assert store.commands[0].approval_threshold == 2
    assert publisher.events == []


@pytest.mark.asyncio
async def test_market_test_send_bypasses_window_and_approval_but_keeps_controls() -> None:
    store = FakeStore()
    publisher = FakePublisher()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=publisher,
        config=PipelineConfig(market_approval_threshold=1),
        clock=lambda: datetime(2026, 7, 11, 22, 0, tzinfo=UTC),
    )
    result = await pipeline.accept(
        ApiAppContext(7, "app", "平台部", frozenset({"market"})),
        SendRequest(
            "market",
            ["13800138000"],
            content="测试回T退订",
            channel="web",
            consent_confirmed=True,
            actor=OPERATOR,
            is_test=True,
        ),
    )
    assert result.status == "queued"
    assert store.commands[0].is_test is True
    assert publisher.events == [("new-batch", "bulk")]


@pytest.mark.asyncio
async def test_storage_failure_refunds_reserved_quota() -> None:
    quota = FakeQuota()
    pipeline = SendPipeline(
        store=FailingStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=quota,
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="系统通知"),
        )
    assert len(quota.reservations) == 1
    assert len(quota.refunds) == 1
    assert quota.refunds[0]["category"] == "notice"


@pytest.mark.asyncio
async def test_save_commit_then_raise_returns_database_fact_without_refund() -> None:
    class CommitThenRaiseIdempotency(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def lookup(self, app_id: int | None, biz_id: str) -> str | None:
            self.lookups += 1
            return "committed-batch" if self.lookups >= 3 else None

    quota = FakeQuota()
    store = FailingStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=CommitThenRaiseIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=quota,
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    result = await pipeline.accept(
        ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
        SendRequest("notice", ["13800138000"], content="通知", biz_id="commit-race"),
    )
    assert result.batch_no == "existing"
    assert quota.refunds == []


@pytest.mark.asyncio
async def test_refund_failure_does_not_mask_storage_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RefundFailureQuota(FakeQuota):
        async def refund_reservation(self, **values: Any) -> None:
            raise RuntimeError("redis refund unavailable")

    pipeline = SendPipeline(
        store=FailingStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=RefundFailureQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="refund-fail"),
        )
    assert "quota reservation compensation unavailable" in caplog.text
