from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.auth.admission_policy import (
    DEFAULT_CONFIG,
    AdmissionLimits,
    AdmissionPolicy,
    AdmissionPolicyRuntime,
    SourceApproval,
    SourceApprovals,
)
from app.core.auth.backends import SessionStateUnavailable
from app.services.runtime_policy import CONFIG_SPECS, InvalidRuntimePolicy, RuntimePolicy
from app.settings import Settings


def approvals(cidr: str = "192.0.2.9/32") -> SourceApprovals:
    return SourceApprovals(
        profiles=(
            SourceApproval(
                cidr=cidr,
                expires_at=datetime.now(UTC) + timedelta(days=1),
                approval_ref="synthetic-test",
            ),
        )
    )


@pytest.mark.parametrize(
    "cidr",
    [
        "0.0.0.0/0",
        "::/0",
        "192.0.2.0/24",
        "2001:db8::/64",
        "::ffff:192.0.2.9/128",
        "0.0.0.0/32",
        "ff00::/128",
    ],
)
def test_world_broad_and_noncanonical_sources_rejected(cidr: str) -> None:
    with pytest.raises(ValueError):
        approvals(cidr)


def test_source_classification_expiry_and_mapped_ip() -> None:
    profile = approvals()
    assert profile.select("192.0.2.9") == "shared"
    assert profile.select("::ffff:192.0.2.9") == "shared"
    for ip in ["192.0.2.8", "0.0.0.0", "invalid", "64:ff9b::c000:209"]:
        assert profile.select(ip) == "internet"
    assert profile.select("192.0.2.9", datetime.now(UTC) + timedelta(days=2)) == "internet"
    v6 = approvals("2001:db8::9/128")
    assert v6.select("2001:0db8:0000::9") == "shared"
    with pytest.raises(ValueError):
        SourceApprovals(profiles=profile.profiles + profile.profiles)


@pytest.mark.parametrize(
    "value",
    [
        '{"version":2}',
        '{"global_burst":17}',
        '{"source_concurrent":9}',
        '{"global_refill_ms":0}',
        '{"global_concurrent":1,"source_concurrent":2}',
        '{"shared_burst":100,"shared_window":20}',
        '{"shared_burst":true}',
        '{"profiles":["192.0.2.9/32"]}',
    ],
)
def test_business_config_cannot_expand_trust_or_hard_bounds(value: str) -> None:
    with pytest.raises(InvalidRuntimePolicy):
        RuntimePolicy.from_mapping({"auth_admission_policy": value})


def test_registered_default_matches_validated_policy() -> None:
    assert CONFIG_SPECS["auth_admission_policy"].default == DEFAULT_CONFIG
    assert AdmissionLimits.model_validate_json(DEFAULT_CONFIG) == AdmissionLimits()


async def test_snapshot_request_reads_never_call_postgres_and_stale_fails_closed() -> None:
    now = [1.0]
    calls = 0
    policy = AdmissionPolicy(1, AdmissionLimits(), approvals())

    class Store:
        async def eval(self, *_args: Any) -> int:
            return 1

    async def load() -> AdmissionPolicy:
        nonlocal calls
        calls += 1
        return policy

    runtime = AdmissionPolicyRuntime(Settings(), Store(), loader=load, clock=lambda: now[0])
    with pytest.raises(SessionStateUnavailable):
        await runtime.load()
    assert calls == 0
    await runtime.ensure_ready()
    for _ in range(100):
        assert await runtime.load() is policy
    assert calls == 1
    now[0] += 16
    with pytest.raises(SessionStateUnavailable):
        await runtime.load()
    assert calls == 1
    await runtime.stop()
    with pytest.raises(SessionStateUnavailable):
        await runtime.load()
