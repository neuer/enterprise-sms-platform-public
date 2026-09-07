"""认证容量与统一 LDAP 失败指标；标签仅允许固定枚举。"""

from __future__ import annotations

from threading import Lock

from prometheus_client import CollectorRegistry, Gauge

_LOCK = Lock()
_ADMIT = {(p, o): 0 for p in ("internet", "shared") for o in ("allowed", "limited", "unavailable")}
_REFUND = {
    (p, o): 0 for p in ("internet", "shared") for o in ("refunded", "skipped", "unavailable")
}
_LDAP = {"count": 0.0, "duration": 0.0, "sink_failure": 0.0, "deadline": 0.0}


def observe_source(profile: str, outcome: str, *, refund: bool = False) -> None:
    with _LOCK:
        target = _REFUND if refund else _ADMIT
        if (profile, outcome) in target:
            target[profile, outcome] += 1


def observe_ldap(outcome: str, seconds: float = 0) -> None:
    with _LOCK:
        if outcome == "credential_uniform":
            _LDAP["count"] += 1
            _LDAP["duration"] += max(0, seconds)
        elif outcome in {"sink_failure", "deadline"}:
            _LDAP[outcome] += 1


def append_capacity_metrics(registry: CollectorRegistry) -> None:
    """向既有隔离 Registry 输出快照，不按来源地址或用户名创建标签。"""

    with _LOCK:
        admit, refund, ldap = dict(_ADMIT), dict(_REFUND), dict(_LDAP)
    for name, values in (("auth_source_admit_total", admit), ("auth_prehash_refund_total", refund)):
        metric = Gauge(
            name,
            "Bounded authentication source outcomes.",
            ("profile", "outcome"),
            registry=registry,
        )
        for (profile, outcome), count in values.items():
            metric.labels(profile=profile, outcome=outcome).set(count)
    for key, name in {
        "count": "ldap_auth_failure_duration_seconds_count",
        "duration": "ldap_auth_failure_duration_seconds_sum",
        "sink_failure": "ldap_timing_sink_failure_total",
        "deadline": "ldap_auth_deadline_exceeded_total",
    }.items():
        Gauge(name, "Uniform LDAP credential failure boundary.", registry=registry).set(ldap[key])
