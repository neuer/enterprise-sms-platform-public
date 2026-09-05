from __future__ import annotations

import asyncio
import base64
import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import SecurityPrincipal, UncertainEffectPrincipal
from app.services.app_ratelimit import ApplicationRateLimitExceeded
from app.services.category import CategoryNotAllowed, policy_for_category
from app.services.crypto import CryptoService, EncryptionContext, ProtectedPhone
from app.services.freq import FrequencyFenceLost
from app.services.idempotency import (
    CLAIM_RELEASE_LUA,
    CLAIM_RENEW_LUA,
    IdempotencyCoordinator,
    IdempotencyFingerprint,
    IdempotencyScope,
)
from app.services.pipeline import (
    AcceptancePreauthorization,
    AcceptCommitConflict,
    AcceptCommitUnknown,
    AllFiltered,
    BatchResponse,
    ConsentRequired,
    IdempotencyConflict,
    InFlightLimitExceeded,
    InFlightQueryUnavailable,
    MarketApiBulkForbidden,
    PipelineConfig,
    SendPipeline,
    SendRequest,
    StoredBatch,
    VendorTestConsoleOnly,
)
from app.services.pipeline_repository import IDEMPOTENCY_LIVE_SQL
from app.services.quota import QuotaFenceLost
from app.services.send_admission import SendAdmissionRejected
from app.services.send_inflight import (
    AcceptCommitResolution,
    _load_bound_batch,
    release_in_flight_reservation,
    release_unbound_acceptance_reservation,
)
from app.services.vendor_test_guard import VendorTestRecipientDenied

ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")
OPERATOR = SecurityPrincipal(2, 20, "operator01", "平台部", "operator")


def _claim_owned(current: str | None, token: str) -> bool:
    return current == token or (current or "").startswith(f"{token}:")


def test_idempotency_live_sql_keeps_unknown_and_unfinished_callback() -> None:
    sql = IDEMPOTENCY_LIVE_SQL.casefold()
    assert "expires_at > now()" in sql
    assert "uncertain" in sql
    assert "unknown_terminal" in sql
    assert "callback_task" in sql
    assert "pending" in sql and "retrying" in sql
    assert "phone" not in sql


def test_inflight_optional_ids_have_explicit_asyncpg_types() -> None:
    release_src = inspect.getsource(release_in_flight_reservation)
    unbound_src = inspect.getsource(release_unbound_acceptance_reservation)
    load_src = inspect.getsource(_load_bound_batch)
    for source in (release_src, unbound_src):
        assert "COALESCE(CAST(:app_id AS BIGINT), app_id)" in source
        assert ":app_id IS NULL" not in source
    assert "CAST(:batch_id AS BIGINT)" in load_src
    assert ":batch_id IS NOT NULL" not in load_src


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
        stored_key_version: int = 1,
    ) -> None:
        self.existing = existing
        self.stored_request_hash = stored_request_hash
        self.stored_key_version = stored_key_version
        self.remembered: list[tuple[IdempotencyScope, str, str]] = []
        self.released: list[str] = []
        self.lookup_calls: list[tuple[IdempotencyScope, str]] = []
        self.inspect_view: Any = None

    async def lookup(self, scope: IdempotencyScope, biz_id: str) -> str | None:
        self.lookup_calls.append((scope, biz_id))
        return self.existing

    async def request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyFingerprint | None:
        if self.stored_request_hash is None:
            return None
        return IdempotencyFingerprint(self.stored_request_hash, self.stored_key_version)

    def claim_key(self, scope: IdempotencyScope, biz_id: str) -> str:
        return f"idem:claim:{scope.key}:{biz_id}"

    def frequency_result_key(
        self, scope: IdempotencyScope, biz_id: str
    ) -> str:
        return f"idem:freq:{scope.key}:{biz_id}"

    def quota_result_key(
        self, scope: IdempotencyScope, biz_id: str, date_key: str
    ) -> str:
        return f"idem:quota:{scope.key}:{biz_id}:{date_key}"

    async def remember(
        self, scope: IdempotencyScope, biz_id: str, batch_no: str
    ) -> None:
        self.remembered.append((scope, biz_id, batch_no))

    async def inspect(self, scope: IdempotencyScope, biz_id: str) -> Any:
        return self.inspect_view

    async def claim(self, scope: IdempotencyScope, biz_id: str, **_kwargs: object) -> str | None:
        return "claim-token"

    async def wait(self, scope: IdempotencyScope, biz_id: str) -> str | None:
        return self.existing

    async def release(
        self, scope: IdempotencyScope, biz_id: str, token: str
    ) -> None:
        self.released.append(token)

    async def renew(
        self, scope: IdempotencyScope, biz_id: str, token: str
    ) -> bool:
        return True

    async def heartbeat(
        self,
        scope: IdempotencyScope,
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

    async def count_in_flight_chunks(self, app_id: int) -> int:
        return 0


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

    async def allow_frequency_many(
        self,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        items: Any,
        limits: Any,
        now: Any = None,
    ) -> list[bool]:
        results = []
        for item in items:
            results.append(
                await self.allow_frequency(
                    reservation_id,
                    category,
                    app_id=app_id,
                    phone_hmac=item.phone_hmac,
                    hmac_aliases=dict(item.hmac_aliases),
                    limits=limits,
                    now=now,
                )
            )
        return results

    async def reserve_quota(self, reservation_id: UUID, **values: Any) -> None:
        self.quota.append({"reservation_id": reservation_id, **values})

    async def request_release(self, reservation_id: UUID, *, event_id: str) -> bool:
        self.releases.append((reservation_id, event_id))
        return True

    async def request_unlinked_release(
        self,
        reservation_id: UUID,
        *,
        event_id: str,
    ) -> bool:
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
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="idempotent-race",
    )
    policy = policy_for_category(request.category, app.allowed_categories)
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
    pipeline.idempotency = FakeIdempotency(
        stored_request_hash=pipeline._request_hash(request, app, policy, key_version=1),
    )

    result = await pipeline.accept(app, request)

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
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="idempotent-race",
    )
    policy = policy_for_category(request.category, app.allowed_categories)
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
    pipeline.idempotency = FakeIdempotency(
        stored_request_hash=pipeline._request_hash(request, app, policy, key_version=1),
    )

    result = await pipeline.accept(app, request)

    assert result.idempotent is True
    assert ledger.releases == []


@pytest.mark.asyncio
async def test_reused_usage_reservation_failure_still_requests_release() -> None:
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

    assert ledger.releases == [
        (
            ledger.reservation_id,
            f"usage:{ledger.reservation_id}:acceptance-failed",
        )
    ]


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
    app = ApiAppContext(7, "app", "平台部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="维护通知",
        biz_id="existing-request",
    )
    policy = policy_for_category(request.category, app.allowed_categories)
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
    pipeline.idempotency = FakeIdempotency(
        "existing",
        stored_request_hash=pipeline._request_hash(request, app, policy, key_version=1),
    )

    result = await pipeline.accept(app, request)

    assert result.idempotent is True
    assert store.commands == []


@pytest.mark.asyncio
async def test_idempotency_short_circuits_frequency_quota_and_storage() -> None:
    store = FakeStore()
    frequency = FakeFrequency()
    quota = FakeQuota()

    class CountingLimiter:
        def __init__(self) -> None:
            self.calls = 0

        async def check(self, **_values: object) -> None:
            self.calls += 1

    app = ApiAppContext(1, "app", "研发部", frozenset({"verify"}))
    request = SendRequest("verify", ["13800138000"], content="验证码123456", biz_id="biz-1")
    policy = policy_for_category(request.category, app.allowed_categories)
    limiter = CountingLimiter()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency("existing"),
        crypto=crypto(),
        frequency=frequency,
        quota=quota,
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=limiter,  # type: ignore[arg-type]
    )
    pipeline.idempotency = FakeIdempotency(
        "existing",
        stored_request_hash=pipeline._request_hash(request, app, policy, key_version=1),
    )
    result = await pipeline.accept(app, request)
    assert result.idempotent is True
    assert limiter.calls == 0
    assert frequency.calls == 0
    assert quota.reservations == []
    assert store.commands == []


@pytest.mark.asyncio
async def test_idempotent_replay_skips_send_limiter_when_bucket_full() -> None:
    class SendFullLimiter:
        def __init__(self) -> None:
            self.checks = 0
            self.replays = 0
            self.costs = 0

        async def check(self, **_values: object) -> None:
            self.checks += 1
            raise ApplicationRateLimitExceeded("应用请求频率超限")

        async def check_replay(self, **_values: object) -> None:
            self.replays += 1

        async def consume_send_cost(self, **_values: object) -> None:
            self.costs += 1
            raise AssertionError("幂等重放不得消耗发送成本")

    app = ApiAppContext(1, "app", "研发部", frozenset({"verify"}))
    request = SendRequest("verify", ["13800138000"], content="验证码123456", biz_id="biz-1")
    policy = policy_for_category(request.category, app.allowed_categories)
    limiter = SendFullLimiter()
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
    pipeline.idempotency = FakeIdempotency(
        "existing",
        stored_request_hash=pipeline._request_hash(request, app, policy, key_version=1),
    )
    result = await pipeline.accept(app, request)
    assert result.idempotent is True
    assert limiter.checks == 0
    assert limiter.replays == 1
    assert limiter.costs == 0


@pytest.mark.asyncio
async def test_idempotent_replay_skips_send_admission_guard() -> None:
    class RecordingGuard:
        def __init__(self) -> None:
            self.calls = 0

        async def authorize(self, **_values: object) -> None:
            self.calls += 1
            raise AssertionError("幂等重放不得走积压准入")

    app = ApiAppContext(1, "app", "研发部", frozenset({"verify"}))
    request = SendRequest("verify", ["13800138000"], content="验证码123456", biz_id="biz-1")
    policy = policy_for_category(request.category, app.allowed_categories)
    guard = RecordingGuard()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency("existing"),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        admission_guard=guard,
    )
    pipeline.idempotency = FakeIdempotency(
        "existing",
        stored_request_hash=pipeline._request_hash(request, app, policy, key_version=1),
    )
    result = await pipeline.accept(app, request)
    assert result.idempotent is True
    assert guard.calls == 0


@pytest.mark.asyncio
async def test_new_send_is_rejected_by_admission_before_request_limiter() -> None:
    class ClosedGuard:
        def __init__(self) -> None:
            self.calls = 0

        async def authorize(self, **values: object) -> None:
            self.calls += 1
            assert values["category"] == "notice"
            raise SendAdmissionRejected("closed", "outbox_backlog", 60)

    class CountingLimiter:
        def __init__(self) -> None:
            self.calls = 0

        async def check(self, **_values: object) -> None:
            self.calls += 1
            raise AssertionError("积压关闭不得再消耗请求限流")

    guard = ClosedGuard()
    limiter = CountingLimiter()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=limiter,  # type: ignore[arg-type]
        admission_guard=guard,
    )
    with pytest.raises(SendAdmissionRejected) as error:
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="new-1"),
        )
    assert error.value.reason == "outbox_backlog"
    assert guard.calls == 1
    assert limiter.calls == 0


@pytest.mark.asyncio
async def test_active_claim_with_different_fingerprint_conflicts_before_admission() -> None:
    from app.services.idempotency import IdempotencyClaimView

    class ClosedGuard:
        async def authorize(self, **_values: object) -> None:
            raise AssertionError("指纹冲突不得走新发送准入")

    idem = FakeIdempotency()
    idem.inspect_view = IdempotencyClaimView("owner", "other-fingerprint", 1)
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=idem,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        admission_guard=ClosedGuard(),  # type: ignore[arg-type]
    )
    with pytest.raises(IdempotencyConflict):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="same-key"),
        )


@pytest.mark.asyncio
async def test_expired_unlimited_quota_is_rejected_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.pipeline import QuotaExemptionExpired

    monkeypatch.setattr(
        "app.services.pipeline.get_settings",
        lambda: SimpleNamespace(environment="production"),
    )
    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(QuotaExemptionExpired):
        await pipeline.accept(
            ApiAppContext(
                1,
                "app",
                "研发部",
                frozenset({"notice"}),
                daily_quota=0,
                unlimited_quota_exempt_until=datetime.now(UTC) - timedelta(seconds=1),
            ),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="expired-1"),
        )
    assert store.commands == []


def test_web_channel_skips_expired_unlimited_quota_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr(
        "app.services.pipeline.get_settings",
        lambda: SimpleNamespace(environment="production"),
    )
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    pipeline._enforce_quota_exemption(
        ApiAppContext(
            1,
            "web-resend",
            "web",
            frozenset({"notice"}),
            daily_quota=0,
            unlimited_quota_exempt_until=datetime.now(UTC) - timedelta(seconds=1),
        ),
        SendRequest(
            "notice",
            ["13800138000"],
            content="通知",
            channel="web",
            biz_id="web-expired-1",
        ),
    )


@pytest.mark.asyncio
async def test_unknown_biz_id_still_hits_send_limiter() -> None:
    class SendFullLimiter:
        async def check(self, **_values: object) -> None:
            raise ApplicationRateLimitExceeded("应用请求频率超限")

        async def check_replay(self, **_values: object) -> None:
            raise AssertionError("未知 biz_id 不得走 replay")

    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=SendFullLimiter(),  # type: ignore[arg-type]
    )
    with pytest.raises(ApplicationRateLimitExceeded):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="random-id-1"),
        )


@pytest.mark.asyncio
async def test_market_api_bulk_requires_explicit_preauthorization() -> None:
    mobiles = [f"1380013{index:04d}" for index in range(50)]
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(market_approval_threshold=50),
    )
    with pytest.raises(MarketApiBulkForbidden, match="未预授权"):
        await pipeline.accept(
            ApiAppContext(7, "app", "平台部", frozenset({"market"})),
            SendRequest("market", mobiles, content="活动通知回T退订", biz_id="mkt-1"),
        )
    allowed = ApiAppContext(
        7,
        "app",
        "平台部",
        frozenset({"market"}),
        allow_market_api_bulk=True,
    )
    result = await pipeline.accept(
        allowed,
        SendRequest("market", mobiles, content="活动通知回T退订", biz_id="mkt-2"),
    )
    assert result.accepted == 50


@pytest.mark.asyncio
async def test_in_flight_chunk_cap_blocks_new_acceptance() -> None:
    class BusyStore(FakeStore):
        async def count_in_flight_chunks(self, app_id: int) -> int:
            assert app_id == 7
            return 200

    pipeline = SendPipeline(
        store=BusyStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(InFlightLimitExceeded):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="busy-1"),
        )


@pytest.mark.asyncio
async def test_in_flight_query_failure_fails_closed() -> None:
    class BrokenStore(FakeStore):
        async def count_in_flight_chunks(self, app_id: int) -> int:
            raise RuntimeError("database unavailable")

    pipeline = SendPipeline(
        store=BrokenStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(InFlightQueryUnavailable):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="busy-2"),
        )


@pytest.mark.asyncio
async def test_unbound_inflight_reservation_releases_on_accept_failure() -> None:
    class TrackingStore(FailingStore):
        def __init__(self) -> None:
            super().__init__()
            self.releases: list[tuple[int, int, str]] = []

        async def reserve_in_flight_chunks(
            self, app_id: int, estimated: int, limit: int
        ) -> object:
            assert app_id == 7
            assert estimated == 1
            assert limit >= 1
            return type("Reservation", (), {"id": 41, "generation": 1})()

        async def release_in_flight_reservation(
            self, reservation_id: int, generation: int, reason: str
        ) -> bool:
            self.releases.append((reservation_id, generation, reason))
            return True

    store = TrackingStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="inflight-1"),
        )
    assert store.releases == [(41, 1, "acceptance-failed")]


class _ReservedFailingStore(FailingStore):
    def __init__(self) -> None:
        super().__init__()
        self.releases: list[tuple[int, int, str]] = []
        self.resolution: AcceptCommitResolution | None = None

    async def reserve_in_flight_chunks(
        self, app_id: int, estimated: int, limit: int
    ) -> object:
        return type("Reservation", (), {"id": 41, "generation": 1})()

    async def release_in_flight_reservation(
        self, reservation_id: int, generation: int, reason: str
    ) -> bool:
        self.releases.append((reservation_id, generation, reason))
        return True

    async def resolve_ambiguous_acceptance_commit(self, **_kwargs: object) -> object:
        assert self.resolution is not None
        return self.resolution


@pytest.mark.asyncio
async def test_ambiguous_commit_keeps_bound_inflight_reservation() -> None:
    store = _ReservedFailingStore()
    store.resolution = AcceptCommitResolution(
        "BOUND_TO_EXPECTED_BATCH",
        batch_no="existing",
    )
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="bound-1")
    policy = policy_for_category(request.category, app.allowed_categories)
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(
            stored_request_hash="pending",
        ),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    pipeline.idempotency.stored_request_hash = pipeline._request_hash(
        request, app, policy, key_version=1
    )
    result = await pipeline.accept(app, request)
    assert result.batch_no == "existing"
    assert store.releases == []


@pytest.mark.asyncio
async def test_unknown_commit_does_not_release_inflight() -> None:
    store = _ReservedFailingStore()
    store.resolution = AcceptCommitResolution("UNKNOWN")
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(AcceptCommitUnknown, match="尚未确认"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="unk-1"),
        )
    assert store.releases == []


@pytest.mark.asyncio
async def test_conflicting_bound_commit_does_not_release_inflight() -> None:
    store = _ReservedFailingStore()
    store.resolution = AcceptCommitResolution("BOUND_TO_CONFLICTING_BATCH")
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(AcceptCommitConflict, match="不一致"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="conf-1"),
        )
    assert store.releases == []


@pytest.mark.asyncio
async def test_unbound_commit_resolution_still_releases_inflight() -> None:
    store = _ReservedFailingStore()
    store.resolution = AcceptCommitResolution("UNBOUND")
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="unb-1"),
        )
    assert store.releases == [(41, 1, "acceptance-failed")]


@pytest.mark.asyncio
async def test_save_failure_lookup_hit_does_not_release_inflight() -> None:
    class CommitThenRaiseIdempotency(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def lookup(
            self, scope: IdempotencyScope, biz_id: str
        ) -> str | None:
            self.lookups += 1
            return "committed-batch" if self.lookups >= 3 else None

    class TrackingFailingStore(FailingStore):
        def __init__(self) -> None:
            super().__init__()
            self.releases: list[tuple[int, int, str]] = []

        async def reserve_in_flight_chunks(
            self, app_id: int, estimated: int, limit: int
        ) -> object:
            return type("Reservation", (), {"id": 41, "generation": 1})()

        async def release_in_flight_reservation(
            self, reservation_id: int, generation: int, reason: str
        ) -> bool:
            self.releases.append((reservation_id, generation, reason))
            return True

    store = TrackingFailingStore()
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="commit-hit")
    policy = policy_for_category(request.category, app.allowed_categories)
    pipeline = SendPipeline(
        store=store,
        idempotency=CommitThenRaiseIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    pipeline.idempotency.stored_request_hash = pipeline._request_hash(
        request, app, policy, key_version=1
    )
    result = await pipeline.accept(app, request)
    assert result.batch_no == "existing"
    assert store.releases == []


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
async def test_retired_fingerprint_key_version_maps_to_conflict_not_param_error() -> None:
    """指纹绑定的 HMAC 版本退役后重试应 409 幂等冲突，不得 400 诱导换 biz_id 重发。"""

    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(
            "existing",
            stored_request_hash="hash-of-retired-version",
            stored_key_version=99,
        ),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )

    with pytest.raises(IdempotencyConflict, match="版本已退役"):
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
    store = FakeStore()
    crypto_service = crypto()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto_service,
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    expected_hash = pipeline._request_hash(request, app, policy, key_version=1)
    idempotency = FakeIdempotency("existing", stored_request_hash=expected_hash)
    pipeline.idempotency = idempotency

    result = await pipeline.accept(app, request)

    assert result.idempotent is True
    assert idempotency.lookup_calls == [
        (IdempotencyScope("account", "1:10"), "web-biz-1")
    ]
    assert store.commands == []


@pytest.mark.asyncio
async def test_console_uat_idempotency_is_bound_to_stable_web_principal() -> None:
    app = ApiAppContext(7, "uat-app", "平台部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        (),
        content="维护通知",
        biz_id="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        channel="web",
        actor=ADMIN,
        is_test=True,
        protected_mobiles=(ProtectedPhone(b"cipher", "a" * 64, "138****8000", 1),),
        protected_hmac_candidates=((1, "a" * 64),),
        vendor_test_uat=True,
    )
    policy = policy_for_category(
        request.category,
        app.allowed_categories,
        notice_blacklist=app.blacklist_check,
    )
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    request_hash = pipeline._request_hash(request, app, policy, key_version=1)
    idempotency = FakeIdempotency("existing", stored_request_hash=request_hash)
    pipeline.idempotency = idempotency

    assert (await pipeline.accept(app, request)).idempotent is True
    assert idempotency.lookup_calls == [
        (IdempotencyScope("account", "1:10"), request.biz_id)
    ]


@pytest.mark.asyncio
async def test_web_idempotency_isolates_different_accounts_with_same_biz_id() -> None:
    store = FakeStore()
    idempotency = FakeIdempotency()
    pipeline = SendPipeline(
        store=store,
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    app = ApiAppContext(0, "web", "平台部", frozenset({"notice"}))
    other = SecurityPrincipal(2, 20, "operator01", "平台部", "operator")

    first = await pipeline.accept(
        app,
        SendRequest(
            "notice",
            ["13800138000"],
            content="维护通知",
            biz_id="shared-biz",
            channel="web",
            actor=ADMIN,
        ),
    )
    second = await pipeline.accept(
        app,
        SendRequest(
            "notice",
            ["13800138000"],
            content="维护通知",
            biz_id="shared-biz",
            channel="web",
            actor=other,
        ),
    )

    assert first.idempotent is False and second.idempotent is False
    assert store.commands[0].scope_id != store.commands[1].scope_id
    assert {item[0] for item in idempotency.remembered} == {
        IdempotencyScope("account", "1:10"),
        IdempotencyScope("account", "2:20"),
    }
    assert len(store.commands) == 2


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
                if not _claim_owned(self.values.get(key), token):
                    return 0
                self.renewals += 1
                self.expires_at[key] = self.now + float(args[0])
                return 1
            assert script == CLAIM_RELEASE_LUA
            if not _claim_owned(self.values.get(key), token):
                return 0
            self.values.pop(key, None)
            self.expires_at.pop(key, None)
            return 1

    class ConcurrentStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.by_biz: dict[str, tuple[str, str]] = {}

        async def exists(
            self, scope: IdempotencyScope, biz_id: str, batch_no: str
        ) -> bool:
            return self.by_biz.get(f"{scope.key}:{biz_id}", (None, None))[0] == batch_no

        async def find_existing(
            self, scope: IdempotencyScope, biz_id: str
        ) -> str | None:
            return self.by_biz.get(f"{scope.key}:{biz_id}", (None, None))[0]

        async def find_request_fingerprint(
            self, scope: IdempotencyScope, biz_id: str
        ) -> IdempotencyFingerprint | None:
            value = self.by_biz.get(f"{scope.key}:{biz_id}", (None, None))[1]
            return IdempotencyFingerprint(value, 1) if value is not None else None

        async def save(self, command: Any) -> StoredBatch:
            self.commands.append(command)
            self.by_biz[
                f"{command.scope_kind}:{command.scope_id}:{command.biz_id}"
            ] = (
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
                return int(_claim_owned(self.values.get(key), token))
            assert _script == CLAIM_RELEASE_LUA
            if not _claim_owned(self.values.get(key), token):
                return 0
            self.values.pop(key, None)
            return 1

    class RepositoryFailingStore(FailingStore):
        async def exists(
            self, scope: IdempotencyScope, biz_id: str, batch_no: str
        ) -> bool:
            return False

        async def find_existing(
            self, scope: IdempotencyScope, biz_id: str
        ) -> str | None:
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
    scope = IdempotencyScope("app", "7")
    assert await coordinator.claim(scope, "retryable-biz") is not None


@pytest.mark.asyncio
async def test_lost_claim_fails_closed_before_business_side_effects() -> None:
    class LostClaimIdempotency(FakeIdempotency):
        async def renew(
            self, scope: IdempotencyScope, biz_id: str, token: str
        ) -> bool:
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
async def test_phone_protect_waves_renew_ownership_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = 0

    async def ownership() -> None:
        nonlocal checks
        checks += 1

    def fake_protect(
        self: SendPipeline,
        phones: Any,
        *,
        blacklist_required: bool,
    ) -> tuple[list[Any], dict[str, Any], dict[str, str], dict[str, dict[int, str]]]:
        return [], {}, {}, {}

    monkeypatch.setattr(SendPipeline, "_protect_plain_phones", fake_protect)
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    await pipeline._protect_plain_phones_batched(
        ["13800138000"] * 4001,
        blacklist_required=False,
        ownership_check=ownership,
    )
    assert checks >= 1


@pytest.mark.asyncio
async def test_replaced_claim_stops_frequency_loop_before_quota_and_storage() -> None:
    ownership = {"token": "claim-token"}

    class ReplacingFrequency(FakeFrequency):
        def __init__(self) -> None:
            super().__init__()
            self.writes = 0

        async def allow(self, category: str, **values: Any) -> bool:
            assert values["claim_key"] == "idem:claim:app:7:freq-race"
            assert values["result_key"] == "idem:freq:app:7:freq-race"
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

        async def renew(
            self, scope: IdempotencyScope, biz_id: str, token: str
        ) -> bool:
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
        async def release(
            self, scope: IdempotencyScope, biz_id: str, token: str
        ) -> None:
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
    assert not hasattr(command, "persisted_content")
    assert service.decrypt_bound_packed_text(
        command.display_content_enc,
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id=command.batch_no,
        ),
    ) == "验证码******"
    assert b"123456" not in command.display_content_enc
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

        async def lookup(
            self, scope: IdempotencyScope, biz_id: str
        ) -> str | None:
            self.lookups += 1
            return "committed-batch" if self.lookups >= 3 else None

    quota = FakeQuota()
    store = FailingStore()
    app = ApiAppContext(1, "app", "研发部", frozenset({"notice"}))
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="commit-race")
    policy = policy_for_category(request.category, app.allowed_categories)
    pipeline = SendPipeline(
        store=store,
        idempotency=CommitThenRaiseIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=quota,
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    pipeline.idempotency.stored_request_hash = pipeline._request_hash(
        request, app, policy, key_version=1
    )
    result = await pipeline.accept(app, request)
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


@pytest.mark.asyncio
async def test_empty_idempotency_fingerprint_conflicts() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency("existing"),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(IdempotencyConflict, match="缺少请求指纹"):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest("notice", ["13800138000"], content="通知", biz_id="legacy-empty"),
        )


@pytest.mark.asyncio
async def test_frequency_batch_uses_allow_frequency_many_and_ownership_check() -> None:
    class CountingLedger(FakeUsageLedger):
        def __init__(self) -> None:
            super().__init__()
            self.many_calls = 0

        async def allow_frequency_many(self, *args: Any, **kwargs: Any) -> list[bool]:
            self.many_calls += 1
            return await super().allow_frequency_many(*args, **kwargs)

    class LosingIdempotency(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.renews = 0

        async def renew(self, scope: IdempotencyScope, biz_id: str, token: str) -> bool:
            self.renews += 1
            return self.renews < 5

    phones = [f"1380013{index:04d}" for index in range(250)]
    ledger = CountingLedger()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=LosingIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        usage_ledger=ledger,
    )
    from app.services.pipeline import IdempotencyClaimLost

    with pytest.raises(IdempotencyClaimLost):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"verify"})),
            SendRequest("verify", phones, content="验证码123456", biz_id="freq-batch"),
        )
    assert ledger.many_calls >= 1
    assert ledger.releases
    assert ledger.releases[0][1].endswith(":acceptance-failed")


@pytest.mark.asyncio
async def test_frequency_many_covers_two_hundred_phones_in_one_ledger_call() -> None:
    class CountingLedger(FakeUsageLedger):
        def __init__(self) -> None:
            super().__init__()
            self.many_calls = 0

        async def allow_frequency_many(self, *args: Any, **kwargs: Any) -> list[bool]:
            self.many_calls += 1
            return await super().allow_frequency_many(*args, **kwargs)

    phones = [f"1380013{index:04d}" for index in range(200)]
    ledger = CountingLedger()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        usage_ledger=ledger,
    )
    result = await pipeline.accept(
        ApiAppContext(1, "app", "研发部", frozenset({"verify"})),
        SendRequest("verify", phones, content="验证码123456", biz_id="freq-200"),
    )
    assert result.accepted == 200
    assert ledger.many_calls == 1
    assert len(ledger.frequency) == 200


@pytest.mark.asyncio
async def test_protect_plain_phones_splits_beyond_one_thousand() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    seen: list[int] = []

    def fake_protect(
        phones: list[str],
        *,
        blacklist_required: bool,
    ) -> tuple[
        list[ProtectedPhone],
        dict[str, frozenset[str]],
        dict[str, str],
        dict[str, dict[int, str]],
    ]:
        seen.append(len(phones))
        assert blacklist_required is False
        return ([], {}, {}, {})

    pipeline._protect_plain_phones = fake_protect  # type: ignore[method-assign]
    phones = [f"1380013{index:04d}" for index in range(1001)]
    protected, candidates, hmacs, aliases = await pipeline._protect_plain_phones_batched(
        phones,
        blacklist_required=False,
    )
    assert seen == [1000, 1]
    assert protected == []
    assert candidates == {}
    assert hmacs == {}
    assert aliases == {}


@pytest.mark.asyncio
async def test_test_send_rejects_explicit_schedule() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="测试发送不支持定时投递"):
        await pipeline.accept(
            ApiAppContext(1, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                is_test=True,
                scheduled_at=datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
            ),
        )


def test_request_hash_normalizes_mobile_order_and_timezone() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    app = ApiAppContext(1, "app", "研发部", frozenset({"notice"}))
    policy = policy_for_category("notice", app.allowed_categories)
    shanghai = datetime(2026, 8, 20, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    left = SendRequest(
        "notice",
        ["13900139000", "13800138000"],
        content="通知",
        scheduled_at=shanghai,
        biz_id="same",
    )
    right = SendRequest(
        "notice",
        ["13800138000", "13900139000"],
        content="通知",
        scheduled_at=shanghai.astimezone(UTC),
        biz_id="same",
    )
    assert pipeline._request_hash(left, app, policy, key_version=1) == pipeline._request_hash(
        right,
        app,
        policy,
        key_version=1,
    )
    assert pipeline._request_hash(
        left,
        app,
        policy,
        key_version=1,
        normalize=False,
    ) != pipeline._request_hash(left, app, policy, key_version=1)


@pytest.mark.asyncio
async def test_legacy_fingerprint_still_matches_same_request() -> None:
    app = ApiAppContext(1, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13900139000", "13800138000"],
        content="通知",
        scheduled_at=datetime(2026, 8, 20, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        biz_id="legacy-hash",
    )
    policy = policy_for_category(request.category, app.allowed_categories)
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    legacy = pipeline._request_hash(
        request,
        app,
        policy,
        key_version=1,
        normalize=False,
    )
    pipeline.idempotency = FakeIdempotency("existing", stored_request_hash=legacy)
    result = await pipeline.accept(app, request)
    assert result.idempotent is True


@pytest.mark.asyncio
async def test_idempotent_fingerprint_conflict_releases_new_usage() -> None:
    ledger = FakeUsageLedger()
    pipeline = SendPipeline(
        store=IdempotentStore(),
        idempotency=FakeIdempotency(stored_request_hash="other-hash"),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )
    with pytest.raises(IdempotencyConflict, match="不同请求"):
        await pipeline.accept(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="conflict-after-save",
            ),
        )
    assert ledger.releases
    assert ledger.releases[0][1].endswith(":idempotent-reuse")


@pytest.mark.asyncio
async def test_save_failure_lookup_error_still_releases_usage() -> None:
    class LookupBoom(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def lookup(self, scope: IdempotencyScope, biz_id: str) -> str | None:
            self.lookups += 1
            if self.lookups >= 3:
                raise RuntimeError("idempotency lookup unavailable")
            return None

    ledger = FakeUsageLedger()
    pipeline = SendPipeline(
        store=FailingStore(),
        idempotency=LookupBoom(),
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
                biz_id="lookup-boom",
            ),
        )
    assert ledger.releases
    assert ledger.releases[0][1].endswith(":acceptance-failed")


@pytest.mark.asyncio
async def test_web_channel_skips_application_rate_limiter() -> None:
    class BoomLimiter:
        async def check(self, **_values: object) -> None:
            raise AssertionError("Web 渠道不得走应用限流桶")

    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=BoomLimiter(),  # type: ignore[arg-type]
    )
    result = await pipeline.accept(
        ApiAppContext(0, "web", "平台部", frozenset({"notice"}), rate_limit_per_min=60),
        SendRequest(
            "notice",
            ["13800138000"],
            content="维护通知",
            biz_id="web-no-app-limit",
            channel="web",
            actor=ADMIN,
        ),
    )
    assert result.batch_no == "new-batch"

@pytest.mark.asyncio
async def test_uncertain_effect_principal_requires_verified_resolution() -> None:
    class ClosedStore(FakeStore):
        async def verify_uncertain_effect(self, principal: UncertainEffectPrincipal) -> None:
            raise ValueError("system resend principal is not forgeable")

    pipeline = SendPipeline(
        store=ClosedStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(ValueError, match="system resend principal is not forgeable"):
        await pipeline.accept(
            ApiAppContext(0, "web", "平台部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="维护通知",
                biz_id="manual-resend:4:2",
                channel="web",
                actor=UncertainEffectPrincipal(4, 1, 2, 2, "平台部"),
            ),
        )


@pytest.mark.asyncio
async def test_uncertain_effect_principal_creates_child_after_verify() -> None:
    class OpenStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.verified: list[UncertainEffectPrincipal] = []

        async def verify_uncertain_effect(self, principal: UncertainEffectPrincipal) -> None:
            self.verified.append(principal)

    store = OpenStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    result = await pipeline.accept(
        ApiAppContext(0, "web", "平台部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="维护通知",
            biz_id="manual-resend:4:2",
            channel="web",
            actor=UncertainEffectPrincipal(4, 1, 2, 2, "平台部"),
        ),
    )
    assert result.batch_no == "new-batch"
    assert store.verified[0].resolution_id == 4
    assert store.commands[0].principal.actor_name == "system_resend:4"


@pytest.mark.asyncio
async def test_replay_if_present_returns_existing_without_admission() -> None:
    class BoomAdmission:
        async def authorize(self, **_values: object) -> None:
            raise AssertionError("replay must not authorize new send")

    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="replay-1",
    )
    idempotency = FakeIdempotency(existing="existing")
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        admission_guard=BoomAdmission(),  # type: ignore[arg-type]
    )
    policy = policy_for_category("notice", app.allowed_categories)
    idempotency.stored_request_hash = pipeline._request_hash(
        request, app, policy, key_version=1
    )
    empty = await pipeline.replay_if_present(
        app,
        SendRequest("notice", ["13800138000"], content="通知"),
    )
    assert empty is None
    replayed = await pipeline.replay_if_present(app, request)
    assert replayed is not None
    assert replayed.batch_no == "existing"
    assert replayed.idempotent is True


@pytest.mark.asyncio
async def test_replay_if_present_rejects_fingerprint_mismatch() -> None:
    idempotency = FakeIdempotency()
    idempotency.inspect_view = SimpleNamespace(fingerprint="other-hash")
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    with pytest.raises(IdempotencyConflict):
        await pipeline.replay_if_present(
            ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="replay-conflict",
            ),
        )


@pytest.mark.asyncio
async def test_replay_if_present_waits_for_in_flight_claim() -> None:
    class WaitIdempotency(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.inspect_view = SimpleNamespace(fingerprint="")

        async def lookup(self, scope: IdempotencyScope, biz_id: str) -> str | None:
            return None

        async def wait(self, scope: IdempotencyScope, biz_id: str) -> str | None:
            return "existing"

    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="replay-wait",
    )
    idempotency = WaitIdempotency()
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    policy = policy_for_category("notice", app.allowed_categories)
    idempotency.stored_request_hash = pipeline._request_hash(
        request, app, policy, key_version=1
    )
    replayed = await pipeline.replay_if_present(app, request)
    assert replayed is not None
    assert replayed.batch_no == "existing"


@pytest.mark.asyncio
async def test_replay_if_present_returns_none_when_inspect_empty() -> None:
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    missing = await pipeline.replay_if_present(
        ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="通知",
            biz_id="replay-miss",
        ),
    )
    assert missing is None


@pytest.mark.asyncio
async def test_replay_if_present_returns_none_when_wait_times_out() -> None:
    class TimeoutIdempotency(FakeIdempotency):
        def __init__(self) -> None:
            super().__init__()
            self.inspect_view = SimpleNamespace(fingerprint="")

        async def lookup(self, scope: IdempotencyScope, biz_id: str) -> str | None:
            return None

        async def wait(self, scope: IdempotencyScope, biz_id: str) -> str | None:
            return None

    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=TimeoutIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    missing = await pipeline.replay_if_present(
        ApiAppContext(7, "app", "研发部", frozenset({"notice"})),
        SendRequest(
            "notice",
            ["13800138000"],
            content="通知",
            biz_id="replay-timeout",
        ),
    )
    assert missing is None


@pytest.mark.asyncio
async def test_replay_if_present_consumes_replay_limit_only() -> None:
    class ReplayLimiter:
        def __init__(self) -> None:
            self.checked = 0
            self.replayed = 0

        async def check(self, **_values: object) -> None:
            self.checked += 1

        async def check_replay(self, **_values: object) -> None:
            self.replayed += 1

    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="replay-limit",
    )
    limiter = ReplayLimiter()
    idempotency = FakeIdempotency(existing="existing")
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        acceptance_limiter=limiter,  # type: ignore[arg-type]
    )
    policy = policy_for_category("notice", app.allowed_categories)
    idempotency.stored_request_hash = pipeline._request_hash(
        request, app, policy, key_version=1
    )
    replayed = await pipeline.replay_if_present(app, request)
    assert replayed is not None
    assert limiter.replayed == 1
    assert limiter.checked == 0


@pytest.mark.asyncio
async def test_replay_if_present_rewrites_uat_plaintext_identity() -> None:
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id="replay-uat",
        vendor_test_uat=True,
    )
    idempotency = FakeIdempotency(existing="existing")
    pipeline = SendPipeline(
        store=FakeStore(),
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    rewritten = pipeline._with_uat_replay_identity(request)
    policy = policy_for_category("notice", app.allowed_categories)
    idempotency.stored_request_hash = pipeline._request_hash(
        rewritten, app, policy, key_version=1
    )
    replayed = await pipeline.replay_if_present(app, request)
    assert replayed is not None
    assert rewritten.protected_mobiles
    assert rewritten.mobiles == ()

