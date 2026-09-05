from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.jwt import (
    ACCESS_TOKEN_TYPE,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JwtClaims,
    JwtService,
    ReauthenticationRequired,
)
from app.core.auth.observability import (
    auth_observability_snapshot,
    reset_auth_observability,
)
from app.core.auth.session_policy import (
    AD_SESSION_POLICY_KEY,
    MIN_ACCEPTED_POLICY_KEY,
    AuthSessionPolicy,
    AuthSessionPolicyConflict,
    apply_policy_cas,
    compare_authoritative_policy,
    load_auth_session_policy,
    parse_auth_session_policy,
    publish_auth_session_policy,
)
from app.core.auth.session_policy_sync import (
    AlignedAuthSessionPolicyLoader,
    AuthSessionPolicyReconciler,
)
from app.core.health import AuthSessionPolicyReadinessCheck
from app.services.admin import AdminService, ConfigRow, ConfigUpdate
from app.services.runtime_policy import RuntimePolicy
from tests.test_admin_service import ADMIN, FakeRepository
from tests.test_auth import TAB_ID, FakeKeyValue
from tests.test_auth_ad_deadline import SECRET, ad_claims, seed_policy

_ISSUE_POLICY_MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations/versions/0102_auth_issue_policy_generation.py"
).read_text(encoding="utf-8")


def _policy(
    revision: int,
    minutes: int,
    epoch: int = 0,
    min_accepted: int = 1,
) -> AuthSessionPolicy:
    return AuthSessionPolicy(revision, minutes, epoch, min_accepted)


def _unbound_ad_claims() -> JwtClaims:
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="ad.user",
        display_name="目录用户",
        dept="研发部",
        role="operator",
        security_version=1,
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_auth_observability()


def test_redis_policy_publish_accepts_higher_revision() -> None:
    assert apply_policy_cas(_policy(1, 480), _policy(2, 60)) == "accepted"


def test_redis_policy_publish_is_idempotent_for_same_revision_same_value() -> None:
    assert apply_policy_cas(_policy(2, 60), _policy(2, 60)) == "idempotent"


def test_redis_policy_publish_rejects_lower_revision() -> None:
    with pytest.raises(AuthSessionPolicyConflict) as error:
        apply_policy_cas(_policy(2, 60), _policy(1, 480))
    assert error.value.reason == "stale"


def test_same_revision_different_value_fails_closed() -> None:
    with pytest.raises(AuthSessionPolicyConflict) as error:
        apply_policy_cas(_policy(2, 60), _policy(2, 480))
    assert error.value.reason == "conflict"


def test_legacy_string_policy_is_readable_but_never_rewritten() -> None:
    parsed = parse_auth_session_policy("2:60")
    assert parsed == _policy(2, 60)


@pytest.mark.asyncio
async def test_cas_store_rejects_stale_and_keeps_hash_format() -> None:
    store = FakeKeyValue()
    await publish_auth_session_policy(store, _policy(2, 60, 10))
    with pytest.raises(AuthSessionPolicyConflict):
        await publish_auth_session_policy(store, _policy(1, 480, 11))
    raw = store.values[AD_SESSION_POLICY_KEY]
    assert raw == {
        "revision": 2,
        "ad_session_max_age_minutes": 60,
        "updated_at_epoch": 10,
    }
    assert await load_auth_session_policy(store) == _policy(2, 60, 10)


@pytest.mark.asyncio
async def test_delayed_old_login_cannot_overwrite_new_policy() -> None:
    started = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    store = FakeKeyValue()
    late = JwtService(SECRET, store, clock=lambda: started)
    await late.publish_ad_session_policy(60, version=2)
    pair = await late.issue_pair(_unbound_ad_claims(), TAB_ID)
    policy = await load_auth_session_policy(store)
    assert policy.revision == 2
    assert policy.ad_session_max_age_minutes == 60
    assert await late.verify(pair.token)


@pytest.mark.asyncio
async def test_two_api_instances_converge_to_same_revision() -> None:
    store = FakeKeyValue()
    first = JwtService(SECRET, store)
    second = JwtService(SECRET, store)
    await first.publish_ad_session_policy(480, version=1)
    await second.publish_ad_session_policy(60, version=2)
    with pytest.raises(AuthSessionPolicyConflict):
        await first.publish_ad_session_policy(480, version=1)
    policy = await load_auth_session_policy(store)
    assert policy == AuthSessionPolicy(2, 60, policy.updated_at_epoch)


@pytest.mark.asyncio
async def test_policy_decrease_limits_existing_access_and_refresh() -> None:
    started = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    current = [started + timedelta(minutes=30)]
    store = FakeKeyValue()
    service = await seed_policy(JwtService(SECRET, store, clock=lambda: current[0]))
    access_pair = await service.issue_pair(
        ad_claims(now=started, deadline_s=8 * 3600),
        TAB_ID,
    )
    refresh_pair = await service.issue_pair(
        ad_claims(now=started, deadline_s=8 * 3600),
        TAB_ID,
    )
    await service.publish_ad_session_policy(15, version=2)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(access_pair.token)
    with pytest.raises(ReauthenticationRequired):
        await service.rotate_refresh(refresh_pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_policy_increase_does_not_extend_existing_deadline() -> None:
    started = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    current = [started]
    store = FakeKeyValue()
    service = await seed_policy(JwtService(SECRET, store, clock=lambda: current[0]))
    access_pair = await service.issue_pair(ad_claims(now=started, deadline_s=60), TAB_ID)
    refresh_pair = await service.issue_pair(ad_claims(now=started, deadline_s=60), TAB_ID)
    await service.publish_ad_session_policy(960, version=2)
    current[0] = started + timedelta(seconds=60)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(access_pair.token)
    with pytest.raises(ReauthenticationRequired):
        await service.rotate_refresh(refresh_pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_access_and_refresh_consume_same_policy_snapshot() -> None:
    started = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    current = [started + timedelta(minutes=20)]
    store = FakeKeyValue()
    service = await seed_policy(JwtService(SECRET, store, clock=lambda: current[0]))
    access_pair = await service.issue_pair(
        ad_claims(now=started, deadline_s=8 * 3600),
        TAB_ID,
    )
    refresh_pair = await service.issue_pair(
        ad_claims(now=started, deadline_s=8 * 3600),
        TAB_ID,
    )
    await service.publish_ad_session_policy(15, version=2)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(access_pair.token)
    with pytest.raises(ReauthenticationRequired):
        await service.rotate_refresh(refresh_pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_unrelated_runtime_config_error_does_not_break_ad_refresh() -> None:
    started = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    current = [started]
    store = FakeKeyValue()
    service = await seed_policy(JwtService(SECRET, store, clock=lambda: current[0]))
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=3600), TAB_ID)
    current[0] = started + timedelta(minutes=10)
    rotated = await service.rotate_refresh(pair.refresh_token, TAB_ID)
    assert rotated.refresh_token


@pytest.mark.asyncio
async def test_missing_redis_policy_fails_closed_without_default() -> None:
    started = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: started)
    token = jwt.encode(
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
            "jti": "j" * 32,
            "iat": started.timestamp(),
            "exp": int((started + timedelta(minutes=15)).timestamp()),
            "auth_time": started.timestamp(),
            "reauth_deadline": started.timestamp() + 480 * 60,
            "auth_policy_version": 1,
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "1"},
    )
    with pytest.raises(SessionStateUnavailable):
        await service.verify(token)


@pytest.mark.asyncio
async def test_old_binary_cannot_write_legacy_policy_during_migration() -> None:
    store = FakeKeyValue()
    await publish_auth_session_policy(store, _policy(3, 90, 1))
    store.values[AD_SESSION_POLICY_KEY] = "2:480"
    with pytest.raises(AuthSessionPolicyConflict):
        await publish_auth_session_policy(store, _policy(2, 30, 2))
    assert store.values[AD_SESSION_POLICY_KEY] == "2:480"
    await publish_auth_session_policy(store, _policy(4, 60, 3))
    assert store.values[AD_SESSION_POLICY_KEY]["revision"] == 4


@pytest.mark.asyncio
async def test_redis_key_loss_is_reconciled_from_postgres() -> None:
    store = FakeKeyValue()
    authoritative = _policy(4, 45, 100)

    async def postgres() -> AuthSessionPolicy:
        return authoritative

    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=postgres,
    )
    outcome = await reconciler.reconcile()
    assert outcome == "missing"
    assert await load_auth_session_policy(store) == authoritative


@pytest.mark.asyncio
async def test_redis_revision_ahead_of_postgres_fails_readiness() -> None:
    store = FakeKeyValue()
    await publish_auth_session_policy(store, _policy(5, 30, 1))

    async def postgres() -> AuthSessionPolicy:
        return _policy(2, 30, 1)

    check = AuthSessionPolicyReadinessCheck(
        object(),  # type: ignore[arg-type]
        reconciler=AuthSessionPolicyReconciler(
            store=store,
            postgres_loader=postgres,
        ),
    )
    with pytest.raises(SessionStateUnavailable, match="ahead"):
        await check()


@pytest.mark.asyncio
async def test_postgres_commit_recovers_after_redis_outage() -> None:
    class BrokenThenReady(FakeKeyValue):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def eval(self, script: str, numkeys: int, *args: object) -> object:
            if self.fail:
                raise RuntimeError("redis down")
            return await super().eval(script, numkeys, *args)

    store = BrokenThenReady()
    authoritative = _policy(3, 120, 50)

    async def postgres() -> AuthSessionPolicy:
        return authoritative

    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=postgres,
    )
    with pytest.raises(SessionStateUnavailable):
        await reconciler.reconcile()
    store.fail = False
    assert await reconciler.reconcile() == "missing"
    assert await load_auth_session_policy(store) == authoritative


def test_compare_detects_same_revision_content_conflict() -> None:
    assert compare_authoritative_policy(_policy(2, 60), _policy(2, 480)) == "conflict"


@pytest.mark.asyncio
async def test_admin_policy_update_publishes_revision_after_commit() -> None:
    published: list[AuthSessionPolicy] = []

    class RecordingRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.policy = _policy(1, 480)

        async def list_configs(self) -> tuple[ConfigRow, ...]:
            return (
                ConfigRow(
                    "ad_session_max_age_minutes",
                    str(self.policy.ad_session_max_age_minutes),
                    "int",
                    "AD",
                    None,
                    None,
                ),
                *await super().list_configs(),
            )

        async def update_configs(self, updates, *, principal, ip):
            self.policy = _policy(2, int(updates[0].value or "60"))
            return await super().update_configs(updates, principal=principal, ip=ip)

        async def load_auth_session_policy(self) -> AuthSessionPolicy:
            return self.policy

    async def publisher(policy: AuthSessionPolicy) -> None:
        published.append(policy)

    service = AdminService(
        RecordingRepository(),
        session_policy_publisher=publisher,
    )
    await service.update_configs(
        (ConfigUpdate("ad_session_max_age_minutes", "60"),),
        principal=ADMIN,
        ip="10.0.0.8",
    )
    assert published == [_policy(2, 60)]


def _decode(token: str) -> dict[str, object]:
    return jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        audience=JWT_AUDIENCE,
        options={"verify_exp": False, "verify_iat": False},
    )


@pytest.mark.asyncio
async def test_ad_issue_pair_uses_auth_session_policy_snapshot() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(15, version=8)
    pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    access = _decode(pair.token)
    assert access["reauth_deadline"] == started.timestamp() + 15 * 60
    assert access["auth_policy_version"] == 8


@pytest.mark.asyncio
async def test_ad_issue_pair_binds_actual_policy_revision() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(30, version=4)
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="ad.user",
        display_name="目录用户",
        dept="研发部",
        role="operator",
        security_version=1,
        auth_policy_version=1,
    )
    pair = await service.issue_pair(claims, TAB_ID)
    access = _decode(pair.token)
    refresh = _decode(pair.refresh_token)
    assert access["auth_policy_version"] == 4
    assert refresh["auth_policy_version"] == 4


@pytest.mark.asyncio
async def test_ad_issue_pair_policy_unavailable_fails_before_any_token_or_cookie() -> None:
    store = FakeKeyValue()
    service = JwtService(SECRET, store)
    with pytest.raises(SessionStateUnavailable):
        await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    assert AD_SESSION_POLICY_KEY not in store.values
    assert MIN_ACCEPTED_POLICY_KEY not in store.values
    assert not any(key.startswith("auth:jwt:refresh-family:") for key in store.values)


@pytest.mark.asyncio
async def test_ad_issue_pair_never_falls_back_to_480_minutes() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(15, version=3)
    pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    access = _decode(pair.token)
    assert access["reauth_deadline"] == started.timestamp() + 15 * 60
    assert access["reauth_deadline"] != started.timestamp() + 480 * 60


@pytest.mark.asyncio
async def test_ad_issue_pair_does_not_load_full_runtime_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(mapping: object) -> RuntimePolicy:
        raise AssertionError("full RuntimePolicy must not be loaded")

    monkeypatch.setattr(RuntimePolicy, "from_mapping", boom)
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(45, version=6)
    pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    access = _decode(pair.token)
    assert access["auth_policy_version"] == 6
    assert access["reauth_deadline"] == started.timestamp() + 45 * 60


@pytest.mark.asyncio
async def test_unrelated_runtime_config_error_does_not_change_ad_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(mapping: object) -> RuntimePolicy:
        raise OSError("mail config unavailable")

    monkeypatch.setattr(RuntimePolicy, "from_mapping", boom)
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(15, version=8)
    pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    access = _decode(pair.token)
    assert access["reauth_deadline"] == started.timestamp() + 15 * 60
    assert access["auth_policy_version"] == 8


@pytest.mark.asyncio
async def test_delayed_login_cannot_publish_or_overwrite_session_policy() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(15, version=8)
    before = dict(store.values[AD_SESSION_POLICY_KEY])
    min_before = store.values[MIN_ACCEPTED_POLICY_KEY]
    pair = await service.issue_pair(
        ad_claims(now=started, deadline_s=8 * 3600, auth_time=started.timestamp()),
        TAB_ID,
    )
    assert store.values[AD_SESSION_POLICY_KEY] == before
    assert store.values[MIN_ACCEPTED_POLICY_KEY] == min_before
    access = _decode(pair.token)
    assert access["auth_policy_version"] == 8


@pytest.mark.asyncio
async def test_policy_increase_does_not_extend_session_issued_under_lower_max_age() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    current = [started]
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: current[0])
    await service.publish_ad_session_policy(15, version=8)
    access_pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    refresh_pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    await service.publish_ad_session_policy(480, version=9)
    current[0] = started + timedelta(minutes=15)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(access_pair.token)
    with pytest.raises(ReauthenticationRequired):
        await service.rotate_refresh(refresh_pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_access_and_refresh_carry_same_policy_revision_and_deadline() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(20, version=7)
    pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    access = _decode(pair.token)
    refresh = _decode(pair.refresh_token)
    assert access["auth_time"] == refresh["auth_time"] == started.timestamp()
    assert access["reauth_deadline"] == refresh["reauth_deadline"]
    assert access["auth_policy_version"] == refresh["auth_policy_version"] == 7


@pytest.mark.asyncio
async def test_redis_missing_policy_is_reconciled_or_login_fails_closed() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    with pytest.raises(SessionStateUnavailable):
        await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    authoritative = _policy(4, 45, 100)

    async def postgres() -> AuthSessionPolicy:
        return authoritative

    reconciler = AuthSessionPolicyReconciler(
        store=store,
        postgres_loader=postgres,
    )
    assert await reconciler.reconcile() == "missing"
    pair = await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    assert _decode(pair.token)["auth_policy_version"] == 4


@pytest.mark.asyncio
async def test_redis_ahead_or_conflicting_policy_fails_readiness_and_login() -> None:
    store = FakeKeyValue()
    await publish_auth_session_policy(store, _policy(5, 30, 1))

    async def postgres() -> AuthSessionPolicy:
        return _policy(2, 30, 1)

    loader = AlignedAuthSessionPolicyLoader(store, postgres_loader=postgres)
    service = JwtService(SECRET, store, session_policy_loader=loader.load)
    check = AuthSessionPolicyReadinessCheck(
        object(),  # type: ignore[arg-type]
        reconciler=AuthSessionPolicyReconciler(
            store=store,
            postgres_loader=postgres,
        ),
    )
    with pytest.raises(SessionStateUnavailable, match="ahead"):
        await check()
    with pytest.raises(SessionStateUnavailable, match="ahead"):
        await service.issue_pair(_unbound_ad_claims(), TAB_ID)
    assert not any(key.startswith("auth:jwt:refresh-family:") for key in store.values)


@pytest.mark.asyncio
async def test_risky_pre_fix_ad_sessions_are_revoked_during_release() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: started)
    await service.publish_ad_session_policy(
        480,
        version=2,
        min_accepted_policy_revision=2,
    )
    missing_version = jwt.encode(
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
            "jti": "j" * 32,
            "iat": started.timestamp(),
            "exp": int((started + timedelta(minutes=15)).timestamp()),
            "auth_time": started.timestamp(),
            "reauth_deadline": started.timestamp() + 480 * 60,
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "1"},
    )
    fallback_revision = jwt.encode(
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
            "sid": "t" * 32,
            "jti": "k" * 32,
            "iat": started.timestamp(),
            "exp": int((started + timedelta(minutes=15)).timestamp()),
            "auth_time": started.timestamp(),
            "reauth_deadline": started.timestamp() + 480 * 60,
            "auth_policy_version": 1,
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "1"},
    )
    with pytest.raises(ReauthenticationRequired):
        await service.verify(missing_version)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(fallback_revision)
    assert "min_accepted_policy_revision = revision + 1" in _ISSUE_POLICY_MIGRATION
    assert "security_version = ua.security_version + 1" in _ISSUE_POLICY_MIGRATION
    snapshot = auth_observability_snapshot()
    assert snapshot.legacy_policy_fallback == 0


@pytest.mark.asyncio
async def test_multi_api_instances_issue_against_same_policy_revision() -> None:
    started = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = FakeKeyValue()
    first = JwtService(SECRET, store, clock=lambda: started)
    second = JwtService(SECRET, store, clock=lambda: started)
    await first.publish_ad_session_policy(15, version=8)
    left = await first.issue_pair(_unbound_ad_claims(), TAB_ID)
    right = await second.issue_pair(_unbound_ad_claims(), TAB_ID)
    assert _decode(left.token)["auth_policy_version"] == 8
    assert _decode(right.token)["auth_policy_version"] == 8
    assert _decode(left.token)["reauth_deadline"] == _decode(right.token)["reauth_deadline"]
    snapshot = auth_observability_snapshot()
    assert snapshot.legacy_policy_fallback == 0
    assert dict(snapshot.token_issue_denied)["policy_unavailable"] == 0
