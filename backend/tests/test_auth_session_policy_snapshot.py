from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core.auth.accounts import PlatformAccount
from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.jwt import (
    ACCESS_TOKEN_TYPE,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JwtService,
    ReauthenticationRequired,
)
from app.core.auth.session_policy import AuthSessionPolicy
from app.core.auth.session_policy_sync import (
    POLICY_RECONCILE_BACKOFF_MIN_S,
    POLICY_RECONCILE_TIMEOUT_S,
    POLICY_SNAPSHOT_MAX_STALENESS_S,
    POLICY_SNAPSHOT_REFRESH_INTERVAL_S,
    AuthSessionPolicyReconciler,
    AuthSessionPolicySnapshot,
    AuthSessionPolicySnapshotProvider,
    policy_snapshot_propagation_deadline_s,
    reset_auth_session_policy_runtime,
)
from tests.test_auth import TAB_ID, FakeKeyValue
from tests.test_auth_ad_deadline import SECRET, ad_claims

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_cancelled_waiter_preserves_owned_single_flight() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def loader() -> AuthSessionPolicy:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _policy()

    reconciler = AuthSessionPolicyReconciler(store=FakeKeyValue(), postgres_loader=loader)
    first = asyncio.create_task(reconciler.reconcile())
    await entered.wait()
    owned = reconciler._inflight
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert reconciler._inflight is owned
    second = asyncio.create_task(reconciler.reconcile())
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await second == "missing"
    await reconciler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("periodic", [False, True])
async def test_stop_waits_for_all_owned_probes_and_fences_late_publication(periodic: bool) -> None:
    entered, cancelled, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def loader() -> AuthSessionPolicy:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await cleaned.wait()
        return _policy()

    reconciler = AuthSessionPolicyReconciler(store=FakeKeyValue(), postgres_loader=loader)
    waiter = None
    if periodic:
        reconciler.start()
    else:
        waiter = asyncio.create_task(reconciler.reconcile())
    await entered.wait()
    owned = reconciler._inflight
    stop_a = asyncio.create_task(reconciler.stop())
    await cancelled.wait()
    stop_b = asyncio.create_task(reconciler.stop())
    await asyncio.sleep(0)
    assert not stop_a.done() and not stop_b.done()
    with pytest.raises(SessionStateUnavailable):
        await reconciler.reconcile()
    with pytest.raises(SessionStateUnavailable):
        reconciler.start()
    stop_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_a
    cleaned.set()
    await stop_b
    assert owned is not None and owned.done()
    assert reconciler._task is None and reconciler._inflight is None
    assert reconciler.snapshot.current().health == "unavailable"
    if waiter is not None:
        with pytest.raises(asyncio.CancelledError):
            await waiter
    reconciler.postgres_loader = CountingPostgres(_policy())
    reconciler.start()
    await reconciler.ensure_ready()
    assert reconciler.snapshot.current().health == "ready"
    await reconciler.stop()


@pytest.mark.asyncio
async def test_unobserved_probe_failure_is_consumed() -> None:
    entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def loader() -> AuthSessionPolicy:
        entered.set()
        await release.wait()
        raise SessionStateUnavailable("unavailable")

    reconciler = AuthSessionPolicyReconciler(store=FakeKeyValue(), postgres_loader=loader)
    try:
        waiter = asyncio.create_task(reconciler.reconcile())
        await entered.wait()
        assert reconciler._inflight is not None
        reconciler._inflight.add_done_callback(lambda _: done.set())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await done.wait()
        assert reconciler._inflight is None and not errors
    finally:
        await reconciler.stop()
        loop.set_exception_handler(previous)


def _policy(
    revision: int = 1,
    minutes: int = 480,
    epoch: int = 1,
    min_accepted: int = 1,
) -> AuthSessionPolicy:
    return AuthSessionPolicy(revision, minutes, epoch, min_accepted)


def _account() -> PlatformAccount:
    return PlatformAccount(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="ad.user",
        normalized_login_name="ad.user",
        display_name="目录用户",
        dept="研发部",
        role="operator",
        security_version=1,
        account_enabled=True,
        identity_enabled=True,
        provider_enabled=True,
    )


class CountingPostgres:
    def __init__(self, policy: AuthSessionPolicy | Exception) -> None:
        self.policy = policy
        self.calls = 0

    async def __call__(self) -> AuthSessionPolicy:
        self.calls += 1
        if isinstance(self.policy, Exception):
            raise self.policy
        return self.policy


class CountingAccount:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, account_id: int, identity_id: int) -> PlatformAccount:
        self.calls += 1
        assert (account_id, identity_id) == (8, 18)
        return _account()


class StepSleeper:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._releases: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self._releases.get()

    def release(self) -> None:
        self._releases.put_nowait(None)


def _expired_ad_token(now: datetime, jti: str, *, deadline_s: int = 8 * 3600) -> str:
    issued = now - timedelta(minutes=16)
    return jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": "8",
            "identity_id": 18,
            "provider_code": "ad",
            "login_name": "ad.user",
            "display_name": "目录用户",
            "dept": "研发部",
            "role": "operator",
            "security_version": 1,
            "token_type": ACCESS_TOKEN_TYPE,
            "sid": "s" * 32,
            "jti": jti,
            "iat": issued.timestamp(),
            "exp": int((issued + timedelta(minutes=15)).timestamp()),
            "auth_time": issued.timestamp(),
            "reauth_deadline": issued.timestamp() + deadline_s,
            "auth_policy_version": 1,
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "1"},
    )


async def _warm(
    *,
    policy: AuthSessionPolicy | None = None,
    mono: list[float] | None = None,
    store: FakeKeyValue | None = None,
    postgres: CountingPostgres | None = None,
) -> tuple[
    JwtService,
    AuthSessionPolicySnapshotProvider,
    AuthSessionPolicyReconciler,
    CountingPostgres,
    CountingAccount,
    FakeKeyValue,
    list[float],
    list[datetime],
]:
    selected = policy or _policy()
    clock = [NOW]
    monotonic_now = mono or [1000.0]
    selected_store = store or FakeKeyValue()
    loader = postgres or CountingPostgres(selected)
    snapshot = AuthSessionPolicySnapshotProvider(
        max_staleness_s=POLICY_SNAPSHOT_MAX_STALENESS_S,
        monotonic_clock=lambda: monotonic_now[0],
    )
    reconciler = AuthSessionPolicyReconciler(
        store=selected_store,
        postgres_loader=loader,
        snapshot=snapshot,
        interval_s=POLICY_SNAPSHOT_REFRESH_INTERVAL_S,
        max_staleness_s=POLICY_SNAPSHOT_MAX_STALENESS_S,
        reconcile_timeout_s=POLICY_RECONCILE_TIMEOUT_S,
        monotonic_clock=lambda: monotonic_now[0],
    )
    await reconciler.reconcile()
    accounts = CountingAccount()
    service = JwtService(
        SECRET,
        selected_store,
        clock=lambda: clock[0],
        session_policy_loader=snapshot.load,
        security_session_loader=accounts,
    )
    return service, snapshot, reconciler, loader, accounts, selected_store, monotonic_now, clock


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    reset_auth_session_policy_runtime()
    yield
    reset_auth_session_policy_runtime()


@pytest.mark.asyncio
async def test_expired_ad_access_rejections_do_not_query_policy_sql() -> None:
    service, _snapshot, _reconciler, postgres, accounts, _store, _mono, clock = await _warm()
    tokens = [_expired_ad_token(clock[0], f"{index:032x}") for index in range(100)]
    before = postgres.calls
    results = await asyncio.gather(
        *[service.verify(token) for token in tokens],
        return_exceptions=True,
    )
    assert all(isinstance(result, InvalidCredentials) for result in results)
    assert postgres.calls == before
    assert accounts.calls == 0


@pytest.mark.asyncio
async def test_warm_snapshot_serves_concurrent_verifications_without_pg_reads() -> None:
    service, _snapshot, _reconciler, postgres, accounts, _store, _mono, clock = await _warm()
    pair = await service.issue_pair(ad_claims(now=clock[0], deadline_s=3600), TAB_ID)
    before = postgres.calls
    results = await asyncio.gather(*[service.verify(pair.token) for _ in range(100)])
    assert all(result.account_id == 8 for result in results)
    assert postgres.calls == before
    assert accounts.calls == 100


@pytest.mark.asyncio
async def test_policy_reconciler_is_single_flight() -> None:
    started = asyncio.Event()
    released = asyncio.Event()
    calls = 0

    async def slow() -> AuthSessionPolicy:
        nonlocal calls
        calls += 1
        started.set()
        await released.wait()
        return _policy()

    store = FakeKeyValue()
    snapshot = AuthSessionPolicySnapshotProvider(monotonic_clock=lambda: 1000.0)
    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=slow,
        snapshot=snapshot,
        interval_s=5,
        max_staleness_s=15,
        reconcile_timeout_s=2,
        monotonic_clock=lambda: 1000.0,
    )
    tasks = [asyncio.create_task(reconciler.reconcile()) for _ in range(8)]
    await started.wait()
    await asyncio.sleep(0)
    assert calls == 1
    released.set()
    outcomes = await asyncio.gather(*tasks)
    assert outcomes == ["missing"] * 8
    assert calls == 1


@pytest.mark.asyncio
async def test_cold_or_stale_snapshot_returns_503_without_request_driven_queries() -> None:
    postgres = CountingPostgres(_policy())
    store = FakeKeyValue()
    mono = [1000.0]
    clock = [NOW]
    snapshot = AuthSessionPolicySnapshotProvider(
        max_staleness_s=15,
        monotonic_clock=lambda: mono[0],
    )
    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=postgres,
        snapshot=snapshot,
        interval_s=5,
        max_staleness_s=15,
        reconcile_timeout_s=2,
        monotonic_clock=lambda: mono[0],
    )
    service = JwtService(
        SECRET,
        store,
        clock=lambda: clock[0],
        session_policy_loader=snapshot.load,
    )
    token = _expired_ad_token(clock[0], "a" * 32)
    with pytest.raises(SessionStateUnavailable):
        await service.verify(token)
    assert postgres.calls == 0

    await reconciler.reconcile()
    assert postgres.calls == 1
    mono[0] += 16
    with pytest.raises(SessionStateUnavailable):
        await service.verify(token)
    assert postgres.calls == 1


@pytest.mark.asyncio
async def test_failed_reconcile_does_not_renew_snapshot_freshness() -> None:
    service, snapshot, reconciler, postgres, _accounts, _store, mono, _clock = await _warm()
    verified = snapshot.current().verified_at_monotonic
    generation = snapshot.current().snapshot_generation
    postgres.policy = SessionStateUnavailable("postgres down")
    mono[0] += 1
    with pytest.raises(SessionStateUnavailable):
        await reconciler.reconcile()
    assert snapshot.current().verified_at_monotonic == verified
    assert snapshot.current().health == "ready"
    assert snapshot.current().snapshot_generation == generation


@pytest.mark.asyncio
async def test_known_policy_conflict_invalidates_snapshot() -> None:
    store = FakeKeyValue()
    service, snapshot, reconciler, postgres, _accounts, store, _mono, clock = await _warm(
        store=store
    )
    postgres.policy = _policy(revision=2, minutes=60)
    store.values["auth:ad:session-policy"] = {
        "revision": 2,
        "ad_session_max_age_minutes": 480,
        "updated_at_epoch": 1,
    }
    store.values["auth:ad:min-accepted-policy-revision"] = 2
    before = postgres.calls
    with pytest.raises(SessionStateUnavailable, match="conflict"):
        await reconciler.reconcile()
    assert snapshot.current().health == "conflict"
    token = _expired_ad_token(clock[0], "b" * 32)
    with pytest.raises(SessionStateUnavailable):
        await service.verify(token)
    assert postgres.calls == before + 1


@pytest.mark.asyncio
async def test_policy_probe_backoff_is_independent_of_request_count() -> None:
    postgres = CountingPostgres(SessionStateUnavailable("down"))
    store = FakeKeyValue()
    sleeper = StepSleeper()
    snapshot = AuthSessionPolicySnapshotProvider(monotonic_clock=lambda: 1000.0)
    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=postgres,
        snapshot=snapshot,
        interval_s=5,
        max_staleness_s=15,
        reconcile_timeout_s=2,
        monotonic_clock=lambda: 1000.0,
        sleeper=sleeper,
    )
    service = JwtService(
        SECRET,
        store,
        clock=lambda: NOW,
        session_policy_loader=snapshot.load,
    )
    token = _expired_ad_token(NOW, "c" * 32)
    reconciler.start()
    for _ in range(50):
        if postgres.calls >= 1 and sleeper.sleeps:
            break
        await asyncio.sleep(0)
    assert postgres.calls == 1
    assert sleeper.sleeps[0] == POLICY_RECONCILE_BACKOFF_MIN_S
    for _ in range(100):
        with pytest.raises(SessionStateUnavailable):
            await service.verify(token)
    assert postgres.calls == 1
    sleeper.release()
    for _ in range(50):
        if postgres.calls >= 2:
            break
        await asyncio.sleep(0)
    assert postgres.calls == 2
    await reconciler.stop()


@pytest.mark.asyncio
async def test_policy_decrease_observed_within_configured_code_deadline() -> None:
    assert policy_snapshot_propagation_deadline_s() == (
        POLICY_SNAPSHOT_REFRESH_INTERVAL_S
        + POLICY_RECONCILE_TIMEOUT_S
        + POLICY_SNAPSHOT_MAX_STALENESS_S
    )
    service, _snapshot, reconciler, postgres, _accounts, _store, mono, clock = await _warm()
    pair = await service.issue_pair(ad_claims(now=clock[0], deadline_s=8 * 3600), TAB_ID)
    postgres.policy = _policy(revision=2, minutes=15)
    mono[0] += policy_snapshot_propagation_deadline_s()
    await reconciler.reconcile()
    clock[0] = NOW + timedelta(minutes=20)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_policy_increase_does_not_extend_issued_deadline() -> None:
    service, _snapshot, reconciler, postgres, _accounts, _store, _mono, clock = await _warm(
        policy=_policy(minutes=15)
    )
    pair = await service.issue_pair(ad_claims(now=clock[0], deadline_s=60), TAB_ID)
    postgres.policy = _policy(revision=2, minutes=960)
    await reconciler.reconcile()
    clock[0] = NOW + timedelta(seconds=60)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_policy_revision_and_min_accepted_are_atomic_snapshot() -> None:
    snapshot = AuthSessionPolicySnapshotProvider(monotonic_clock=lambda: 10.0)
    snapshot.publish(
        AuthSessionPolicySnapshot(_policy(3, 60, 1, 3), 10.0, "ready", 1)
    )
    snapshot.publish(
        AuthSessionPolicySnapshot(_policy(2, 480, 1, 1), 11.0, "ready", 2)
    )
    current = snapshot.current()
    assert current.policy is not None
    assert current.policy.revision == 3
    assert current.policy.min_accepted_policy_revision == 3
    loaded = await snapshot.load()
    assert loaded.revision == loaded.min_accepted_policy_revision == 3


@pytest.mark.asyncio
async def test_valid_access_keeps_authoritative_account_verification() -> None:
    service, _snapshot, _reconciler, postgres, accounts, _store, _mono, clock = await _warm()
    pair = await service.issue_pair(ad_claims(now=clock[0], deadline_s=3600), TAB_ID)
    before = postgres.calls
    claims = await service.verify(pair.token)
    assert claims.login_name == "ad.user"
    assert postgres.calls == before
    assert accounts.calls == 1


@pytest.mark.asyncio
async def test_policy_task_start_stop_are_idempotent() -> None:
    postgres = CountingPostgres(_policy())
    store = FakeKeyValue()
    sleeper = StepSleeper()
    snapshot = AuthSessionPolicySnapshotProvider(monotonic_clock=lambda: 1000.0)
    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=postgres,
        snapshot=snapshot,
        interval_s=5,
        max_staleness_s=15,
        reconcile_timeout_s=2,
        monotonic_clock=lambda: 1000.0,
        sleeper=sleeper,
    )
    reconciler.start()
    first = reconciler._task
    reconciler.start()
    assert reconciler._task is first
    await reconciler.stop()
    await reconciler.stop()
    assert reconciler._task is None
    reconciler.start()
    assert reconciler._task is not None
    assert reconciler._task is not first
    await reconciler.stop()
