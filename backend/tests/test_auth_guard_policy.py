from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.guard_policy import AUTH_GUARD_KEYS, AuthGuardPolicy, SqlAuthGuardPolicyLoader
from app.core.auth.observability import reset_auth_observability
from app.core.auth.providers import AuthProviderRegistry
from app.core.auth.service import AuthService, LoginGuard, RateLimited
from app.services.runtime_policy import InvalidRuntimePolicy
from tests.test_auth import (
    FakeKeyValue,
    FakeProviderRepository,
    RecordingProviderKind,
    provider_record,
)


class CountingPolicyLoader:
    def __init__(self, policy: AuthGuardPolicy | None = None) -> None:
        self.calls = 0
        self.policy = policy or AuthGuardPolicy.defaults()

    async def load(self) -> AuthGuardPolicy:
        self.calls += 1
        return self.policy


def _service(
    store: FakeKeyValue,
    loader: CountingPolicyLoader,
    *,
    valid: str = "correct",
) -> AuthService:
    local = RecordingProviderKind("local", valid_password=valid)
    return AuthService(
        AuthProviderRegistry(
            FakeProviderRepository(provider_record(code="local", kind="local")),
            {"local": local},
        ),
        LoginGuard(store, policy_loader=loader.load),
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_auth_observability()


@pytest.mark.asyncio
async def test_banned_ip_admission_performs_zero_policy_database_reads() -> None:
    store = FakeKeyValue()
    store.values["auth:ban:ip:10.0.0.8"] = "8a5a77a4-286f-4d81-9a64-5379e30df986"
    loader = CountingPolicyLoader()
    service = _service(store, loader)

    with pytest.raises(RateLimited):
        await service.authenticate("local", "user01", "correct", "10.0.0.8")

    assert loader.calls == 0


@pytest.mark.asyncio
async def test_prehash_burst_rejection_performs_zero_policy_database_reads() -> None:
    store = FakeKeyValue()
    store.values["auth:prehash:bucket:ip:10.0.0.9"] = 5
    loader = CountingPolicyLoader()
    service = _service(store, loader)

    with pytest.raises(RateLimited):
        await service.authenticate("local", "user01", "correct", "10.0.0.9")

    assert loader.calls == 0


@pytest.mark.asyncio
async def test_prehash_window_rejection_performs_zero_policy_database_reads() -> None:
    store = FakeKeyValue()
    store.values["auth:prehash:window:ip:10.0.0.10"] = 20
    loader = CountingPolicyLoader()
    service = _service(store, loader)

    with pytest.raises(RateLimited):
        await service.authenticate("local", "user01", "correct", "10.0.0.10")

    assert loader.calls == 0


@pytest.mark.asyncio
async def test_successful_login_performs_zero_auth_policy_database_reads() -> None:
    store = FakeKeyValue()
    loader = CountingPolicyLoader()
    service = _service(store, loader)

    identity = await service.authenticate("local", "user01", "correct", "10.0.0.11")

    assert identity.provider_code == "local"
    assert loader.calls == 0


@pytest.mark.asyncio
async def test_invalid_credentials_loads_auth_policy_at_most_once_per_attempt() -> None:
    store = FakeKeyValue()
    loader = CountingPolicyLoader()
    service = _service(store, loader)

    with pytest.raises(InvalidCredentials):
        await service.authenticate("local", "user01", "wrong", "10.0.0.12")

    assert loader.calls == 1


def test_auth_policy_query_reads_only_four_guard_keys() -> None:
    assert AUTH_GUARD_KEYS == (
        "login_fail_limit",
        "login_lock_minutes",
        "login_ip_fail_limit",
        "login_ip_ban_minutes",
    )
    policy = AuthGuardPolicy.from_mapping(
        {
            "login_fail_limit": "5",
            "login_lock_minutes": "15",
            "login_ip_fail_limit": "20",
            "login_ip_ban_minutes": "15",
            "vendor_qps": "boom",
            "alert_mail_to": "ops@example.invalid",
        }
    )
    assert policy.login_fail_limit == 5
    assert not hasattr(policy, "vendor_qps")


def test_unrelated_runtime_policy_error_does_not_break_login() -> None:
    policy = AuthGuardPolicy.from_mapping(
        {
            "login_fail_limit": "5",
            "callback_allow_cidrs": "not-a-cidr",
            "market_send_window": "invalid",
        }
    )
    assert policy.login_fail_limit == 5


def test_auth_policy_unknown_version_fails_closed() -> None:
    with pytest.raises(InvalidRuntimePolicy):
        AuthGuardPolicy.from_mapping({}, version=0)


@pytest.mark.asyncio
async def test_auth_policy_stale_window_is_bounded() -> None:
    clock = [100.0]

    class Loader(SqlAuthGuardPolicyLoader):
        def __init__(self) -> None:
            super().__init__(
                settings=SimpleNamespace(),
                cache_ttl_s=15,
                stale_window_s=60,
                clock=lambda: clock[0],
            )
            self._cached = AuthGuardPolicy.defaults()
            self._cached_at = 100.0
            self.loads = 0

        async def _load_fresh(self) -> AuthGuardPolicy:
            self.loads += 1
            raise RuntimeError("sys_config unavailable")

    loader = Loader()
    clock[0] = 150.0
    snapshot = await loader.load()
    assert snapshot == AuthGuardPolicy.defaults()
    assert loader.loads == 1

    clock[0] = 161.0
    with pytest.raises(SessionStateUnavailable):
        await loader.load()


def test_multi_worker_policy_version_is_consistent() -> None:
    rows = [
        ("login_fail_limit", "5", None),
        ("login_lock_minutes", "15", None),
        ("login_ip_fail_limit", "20", None),
        ("login_ip_ban_minutes", "15", None),
    ]
    from app.core.auth.guard_policy import _version_from_rows

    assert _version_from_rows(rows) == _version_from_rows(list(reversed(rows)))


def test_login_attack_does_not_exhaust_sms_accept_pool() -> None:
    import inspect

    source = inspect.getsource(SqlAuthGuardPolicyLoader._load_fresh)
    assert 'database_url_for("auth")' in source
    assert "sms_accept" not in source
