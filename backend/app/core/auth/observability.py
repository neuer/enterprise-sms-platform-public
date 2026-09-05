"""认证控制面的低基数进程指标；禁止用户名、IP、UUID 或 Token 标签。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import time
from typing import Literal

TransitionAction = Literal["auth_account_locked", "auth_ip_banned"]
TransitionOwner = Literal["api", "reconciler"]
TransitionErrorClass = Literal["timeout", "unavailable", "other"]
_ACTIONS: tuple[TransitionAction, ...] = ("auth_account_locked", "auth_ip_banned")
_OWNERS: tuple[TransitionOwner, ...] = ("api", "reconciler")
_ERROR_CLASSES: tuple[TransitionErrorClass, ...] = ("timeout", "unavailable", "other")
_LOCK = Lock()
_CREATED = {action: 0 for action in _ACTIONS}
_SUCCESS = {action: 0 for action in _ACTIONS}
_FAILURE = {action: 0 for action in _ACTIONS}
_CLAIM = {(action, owner): 0 for action in _ACTIONS for owner in _OWNERS}
_LEASE_EXPIRED = {action: 0 for action in _ACTIONS}
_RETRY = {(action, error): 0 for action in _ACTIONS for error in _ERROR_CLASSES}
_DEAD = {action: 0 for action in _ACTIONS}
_DURATION = {action: 0.0 for action in _ACTIONS}
_PENDING = 0
_OLDEST_PENDING = 0.0
_POLICY_HIT = 0
_POLICY_MISS = 0
_POLICY_FAILURE = 0
_GUARD_DB_QUERIES = 0
_POLICY_AGE = 0.0
_ADMIT = {"allowed": 0, "banned": 0, "prehash": 0, "unavailable": 0}
_SESSION_POLICY_PUBLISH = {
    "accepted": 0,
    "idempotent": 0,
    "stale": 0,
    "conflict": 0,
    "invalid": 0,
    "unavailable": 0,
}
_SESSION_POLICY_RECONCILE = {
    "aligned": 0,
    "missing": 0,
    "behind": 0,
    "ahead": 0,
    "conflict": 0,
    "unavailable": 0,
}
_SESSION_POLICY_CONFLICT = {"stale": 0, "conflict": 0, "ahead": 0, "mismatch": 0}
_SESSION_POLICY_REVISION = {"postgres": 0, "redis": 0}
_SESSION_POLICY_SNAPSHOT_AGE = 0.0
_SESSION_POLICY_LAG = 0.0


@dataclass(frozen=True, slots=True)
class AuthObservabilitySnapshot:
    transition_created: tuple[tuple[str, int], ...]
    transition_success: tuple[tuple[str, int], ...]
    transition_failure: tuple[tuple[str, int], ...]
    policy_cache_hit: int
    policy_cache_miss: int
    policy_load_failure: int
    guard_db_queries: int
    policy_snapshot_age_seconds: float
    admit: tuple[tuple[str, int], ...]
    transition_pending: int = 0
    transition_oldest_pending_seconds: float = 0.0
    transition_claim: tuple[tuple[str, str, int], ...] = ()
    transition_lease_expired: tuple[tuple[str, int], ...] = ()
    transition_retry: tuple[tuple[str, str, int], ...] = ()
    transition_dead: tuple[tuple[str, int], ...] = ()
    transition_database_duration_seconds: tuple[tuple[str, float], ...] = ()
    session_policy_revision: tuple[tuple[str, int], ...] = ()
    session_policy_publish: tuple[tuple[str, int], ...] = ()
    session_policy_reconcile: tuple[tuple[str, int], ...] = ()
    session_policy_conflict: tuple[tuple[str, int], ...] = ()
    session_policy_snapshot_age_seconds: float = 0.0
    session_policy_publish_lag_seconds: float = 0.0


def observe_transition_created(action: TransitionAction) -> None:
    with _LOCK:
        _CREATED[action] += 1


def observe_transition_success(action: TransitionAction) -> None:
    with _LOCK:
        _SUCCESS[action] += 1


def observe_transition_failure(action: TransitionAction) -> None:
    with _LOCK:
        _FAILURE[action] += 1


def observe_transition_claim(action: TransitionAction, owner: TransitionOwner) -> None:
    with _LOCK:
        _CLAIM[action, owner] += 1


def observe_transition_lease_expired(action: TransitionAction) -> None:
    with _LOCK:
        _LEASE_EXPIRED[action] += 1


def observe_transition_retry(action: TransitionAction, error_class: TransitionErrorClass) -> None:
    key = error_class if error_class in _ERROR_CLASSES else "other"
    with _LOCK:
        _RETRY[action, key] += 1


def observe_transition_dead(action: TransitionAction) -> None:
    with _LOCK:
        _DEAD[action] += 1


def observe_transition_database_duration(action: TransitionAction, seconds: float) -> None:
    with _LOCK:
        _DURATION[action] = max(0.0, seconds)


def observe_transition_pending(count: int, oldest_seconds: float) -> None:
    global _PENDING, _OLDEST_PENDING
    with _LOCK:
        _PENDING = max(0, count)
        _OLDEST_PENDING = max(0.0, oldest_seconds)


def observe_policy_cache_hit() -> None:
    global _POLICY_HIT
    with _LOCK:
        _POLICY_HIT += 1


def observe_policy_cache_miss() -> None:
    global _POLICY_MISS
    with _LOCK:
        _POLICY_MISS += 1


def observe_policy_load_failure() -> None:
    global _POLICY_FAILURE
    with _LOCK:
        _POLICY_FAILURE += 1


def observe_guard_db_query() -> None:
    global _GUARD_DB_QUERIES
    with _LOCK:
        _GUARD_DB_QUERIES += 1


def observe_policy_snapshot_age(age_seconds: float) -> None:
    global _POLICY_AGE
    with _LOCK:
        _POLICY_AGE = max(0.0, age_seconds)


def observe_admit(outcome: Literal["allowed", "banned", "prehash", "unavailable"]) -> None:
    with _LOCK:
        _ADMIT[outcome] += 1


def observe_session_policy_publish(outcome: str) -> None:
    key = outcome if outcome in _SESSION_POLICY_PUBLISH else "unavailable"
    with _LOCK:
        _SESSION_POLICY_PUBLISH[key] += 1


def observe_session_policy_reconcile(outcome: str) -> None:
    key = outcome if outcome in _SESSION_POLICY_RECONCILE else "unavailable"
    with _LOCK:
        _SESSION_POLICY_RECONCILE[key] += 1


def observe_session_policy_conflict(conflict_type: str) -> None:
    key = conflict_type if conflict_type in _SESSION_POLICY_CONFLICT else "conflict"
    with _LOCK:
        _SESSION_POLICY_CONFLICT[key] += 1


def observe_session_policy_revisions(
    *,
    postgres_revision: int | None = None,
    redis_revision: int | None = None,
) -> None:
    with _LOCK:
        if postgres_revision is not None:
            _SESSION_POLICY_REVISION["postgres"] = max(0, postgres_revision)
        if redis_revision is not None:
            _SESSION_POLICY_REVISION["redis"] = max(0, redis_revision)


def observe_session_policy_publish_lag(seconds: float) -> None:
    global _SESSION_POLICY_LAG
    with _LOCK:
        _SESSION_POLICY_LAG = max(0.0, seconds)


def observe_session_policy_snapshot_age(
    updated_at_epoch: float,
    *,
    now: float | None = None,
) -> None:
    global _SESSION_POLICY_SNAPSHOT_AGE
    current = time() if now is None else now
    with _LOCK:
        _SESSION_POLICY_SNAPSHOT_AGE = max(0.0, current - float(updated_at_epoch))


def auth_observability_snapshot() -> AuthObservabilitySnapshot:
    with _LOCK:
        return AuthObservabilitySnapshot(
            tuple((action, _CREATED[action]) for action in _ACTIONS),
            tuple((action, _SUCCESS[action]) for action in _ACTIONS),
            tuple((action, _FAILURE[action]) for action in _ACTIONS),
            _POLICY_HIT,
            _POLICY_MISS,
            _POLICY_FAILURE,
            _GUARD_DB_QUERIES,
            _POLICY_AGE,
            tuple(_ADMIT.items()),
            _PENDING,
            _OLDEST_PENDING,
            tuple(
                (action, owner, _CLAIM[action, owner])
                for action in _ACTIONS
                for owner in _OWNERS
            ),
            tuple((action, _LEASE_EXPIRED[action]) for action in _ACTIONS),
            tuple(
                (action, error, _RETRY[action, error])
                for action in _ACTIONS
                for error in _ERROR_CLASSES
            ),
            tuple((action, _DEAD[action]) for action in _ACTIONS),
            tuple((action, _DURATION[action]) for action in _ACTIONS),
            tuple(_SESSION_POLICY_REVISION.items()),
            tuple(_SESSION_POLICY_PUBLISH.items()),
            tuple(_SESSION_POLICY_RECONCILE.items()),
            tuple(_SESSION_POLICY_CONFLICT.items()),
            _SESSION_POLICY_SNAPSHOT_AGE,
            _SESSION_POLICY_LAG,
        )


def reset_auth_observability() -> None:
    """仅供测试重置进程计数。"""

    global _POLICY_HIT, _POLICY_MISS, _POLICY_FAILURE, _GUARD_DB_QUERIES, _POLICY_AGE
    global _PENDING, _OLDEST_PENDING
    global _SESSION_POLICY_SNAPSHOT_AGE, _SESSION_POLICY_LAG
    with _LOCK:
        for action in _ACTIONS:
            _CREATED[action] = 0
            _SUCCESS[action] = 0
            _FAILURE[action] = 0
            _LEASE_EXPIRED[action] = 0
            _DEAD[action] = 0
            _DURATION[action] = 0.0
            for owner in _OWNERS:
                _CLAIM[action, owner] = 0
            for error in _ERROR_CLASSES:
                _RETRY[action, error] = 0
        _PENDING = 0
        _OLDEST_PENDING = 0.0
        _POLICY_HIT = 0
        _POLICY_MISS = 0
        _POLICY_FAILURE = 0
        _GUARD_DB_QUERIES = 0
        _POLICY_AGE = 0.0
        for key in _ADMIT:
            _ADMIT[key] = 0
        for key in _SESSION_POLICY_PUBLISH:
            _SESSION_POLICY_PUBLISH[key] = 0
        for key in _SESSION_POLICY_RECONCILE:
            _SESSION_POLICY_RECONCILE[key] = 0
        for key in _SESSION_POLICY_CONFLICT:
            _SESSION_POLICY_CONFLICT[key] = 0
        _SESSION_POLICY_REVISION["postgres"] = 0
        _SESSION_POLICY_REVISION["redis"] = 0
        _SESSION_POLICY_SNAPSHOT_AGE = 0.0
        _SESSION_POLICY_LAG = 0.0
