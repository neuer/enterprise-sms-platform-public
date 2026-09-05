"""认证控制面的低基数进程指标；禁止用户名、IP、UUID 或 Token 标签。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import time
from typing import Literal

TransitionAction = Literal["auth_account_locked", "auth_ip_banned"]
TransitionOwner = Literal["api", "reconciler"]
TransitionErrorClass = Literal["timeout", "unavailable", "other"]
OrphanReason = Literal["missing_hash", "incomplete_envelope", "id_mismatch"]
IntegrityDirection = Literal["hash_to_due", "due_to_hash"]
IntegrityOutcome = Literal["repaired", "skipped", "orphaned"]
EnvelopeFieldClass = Literal[
    "action",
    "provider",
    "result",
    "count",
    "ttl",
    "ip",
    "schema",
    "created",
    "id",
    "missing_hash",
]
_ACTIONS: tuple[TransitionAction, ...] = ("auth_account_locked", "auth_ip_banned")
_OWNERS: tuple[TransitionOwner, ...] = ("api", "reconciler")
_ERROR_CLASSES: tuple[TransitionErrorClass, ...] = ("timeout", "unavailable", "other")
_ORPHAN_REASONS: tuple[OrphanReason, ...] = (
    "missing_hash",
    "incomplete_envelope",
    "id_mismatch",
)
_INTEGRITY_DIRECTIONS: tuple[IntegrityDirection, ...] = ("hash_to_due", "due_to_hash")
_INTEGRITY_OUTCOMES: tuple[IntegrityOutcome, ...] = ("repaired", "skipped", "orphaned")
_FIELD_CLASSES: tuple[EnvelopeFieldClass, ...] = (
    "action",
    "provider",
    "result",
    "count",
    "ttl",
    "ip",
    "schema",
    "created",
    "id",
    "missing_hash",
)
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
_ORPHAN = {reason: 0 for reason in _ORPHAN_REASONS}
_INTEGRITY = {
    (direction, outcome): 0
    for direction in _INTEGRITY_DIRECTIONS
    for outcome in _INTEGRITY_OUTCOMES
}
_ENVELOPE_INVALID = {field: 0 for field in _FIELD_CLASSES}
_DEAD_LETTER = {reason: 0 for reason in _ORPHAN_REASONS}
_PENDING_WITHOUT_DUE = 0
_DUE_WITHOUT_PAYLOAD = 0
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
_TOKEN_ISSUE_POLICY_REVISION = {"ad": 0, "local": 0}
_TOKEN_ISSUE_POLICY_LOAD = {"success": 0, "unavailable": 0, "invalid": 0}
_TOKEN_ISSUE_POLICY_MISMATCH = {"revision": 0, "deadline": 0}
_TOKEN_ISSUE_DENIED = {"policy_unavailable": 0}
_LEGACY_POLICY_FALLBACK = 0


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
    token_issue_policy_revision: tuple[tuple[str, int], ...] = ()
    token_issue_policy_load: tuple[tuple[str, int], ...] = ()
    token_issue_policy_mismatch: tuple[tuple[str, int], ...] = ()
    token_issue_denied: tuple[tuple[str, int], ...] = ()
    legacy_policy_fallback: int = 0
    transition_orphan: tuple[tuple[str, int], ...] = ()
    transition_integrity_repair: tuple[tuple[str, str, int], ...] = ()
    transition_envelope_invalid: tuple[tuple[str, int], ...] = ()
    transition_pending_without_due: int = 0
    transition_due_without_payload: int = 0
    transition_dead_letter: tuple[tuple[str, int], ...] = ()


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


def observe_transition_orphan(reason: str) -> None:
    key: OrphanReason = reason if reason in _ORPHAN_REASONS else "incomplete_envelope"
    with _LOCK:
        _ORPHAN[key] += 1


def observe_transition_integrity_repair(direction: str, outcome: str) -> None:
    dir_key: IntegrityDirection = (
        direction if direction in _INTEGRITY_DIRECTIONS else "hash_to_due"
    )
    out_key: IntegrityOutcome = outcome if outcome in _INTEGRITY_OUTCOMES else "skipped"
    with _LOCK:
        _INTEGRITY[dir_key, out_key] += 1


def observe_transition_envelope_invalid(field_class: str) -> None:
    key: EnvelopeFieldClass = (
        field_class if field_class in _FIELD_CLASSES else "schema"
    )
    with _LOCK:
        _ENVELOPE_INVALID[key] += 1


def observe_transition_dead_letter(reason: str) -> None:
    key: OrphanReason = reason if reason in _ORPHAN_REASONS else "incomplete_envelope"
    with _LOCK:
        _DEAD_LETTER[key] += 1


def observe_transition_integrity_gauges(
    *,
    pending_without_due: int,
    due_without_payload: int,
) -> None:
    global _PENDING_WITHOUT_DUE, _DUE_WITHOUT_PAYLOAD
    with _LOCK:
        _PENDING_WITHOUT_DUE = max(0, pending_without_due)
        _DUE_WITHOUT_PAYLOAD = max(0, due_without_payload)


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


def observe_token_issue_policy_revision(provider: str, revision: int) -> None:
    key = provider if provider in _TOKEN_ISSUE_POLICY_REVISION else "ad"
    with _LOCK:
        _TOKEN_ISSUE_POLICY_REVISION[key] = max(0, revision)


def observe_token_issue_policy_load(outcome: str) -> None:
    key = outcome if outcome in _TOKEN_ISSUE_POLICY_LOAD else "unavailable"
    with _LOCK:
        _TOKEN_ISSUE_POLICY_LOAD[key] += 1


def observe_token_issue_policy_mismatch(mismatch_type: str) -> None:
    key = mismatch_type if mismatch_type in _TOKEN_ISSUE_POLICY_MISMATCH else "revision"
    with _LOCK:
        _TOKEN_ISSUE_POLICY_MISMATCH[key] += 1


def observe_token_issue_denied(reason: str) -> None:
    key = reason if reason in _TOKEN_ISSUE_DENIED else "policy_unavailable"
    with _LOCK:
        _TOKEN_ISSUE_DENIED[key] += 1


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
            tuple(_TOKEN_ISSUE_POLICY_REVISION.items()),
            tuple(_TOKEN_ISSUE_POLICY_LOAD.items()),
            tuple(_TOKEN_ISSUE_POLICY_MISMATCH.items()),
            tuple(_TOKEN_ISSUE_DENIED.items()),
            _LEGACY_POLICY_FALLBACK,
            tuple((reason, _ORPHAN[reason]) for reason in _ORPHAN_REASONS),
            tuple(
                (direction, outcome, _INTEGRITY[direction, outcome])
                for direction in _INTEGRITY_DIRECTIONS
                for outcome in _INTEGRITY_OUTCOMES
            ),
            tuple((field, _ENVELOPE_INVALID[field]) for field in _FIELD_CLASSES),
            _PENDING_WITHOUT_DUE,
            _DUE_WITHOUT_PAYLOAD,
            tuple((reason, _DEAD_LETTER[reason]) for reason in _ORPHAN_REASONS),
        )


def reset_auth_observability() -> None:
    """仅供测试重置进程计数。"""

    global _POLICY_HIT, _POLICY_MISS, _POLICY_FAILURE, _GUARD_DB_QUERIES, _POLICY_AGE
    global _PENDING, _OLDEST_PENDING
    global _SESSION_POLICY_SNAPSHOT_AGE, _SESSION_POLICY_LAG
    global _LEGACY_POLICY_FALLBACK
    global _PENDING_WITHOUT_DUE, _DUE_WITHOUT_PAYLOAD
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
        for key in _TOKEN_ISSUE_POLICY_REVISION:
            _TOKEN_ISSUE_POLICY_REVISION[key] = 0
        for key in _TOKEN_ISSUE_POLICY_LOAD:
            _TOKEN_ISSUE_POLICY_LOAD[key] = 0
        for key in _TOKEN_ISSUE_POLICY_MISMATCH:
            _TOKEN_ISSUE_POLICY_MISMATCH[key] = 0
        for key in _TOKEN_ISSUE_DENIED:
            _TOKEN_ISSUE_DENIED[key] = 0
        _LEGACY_POLICY_FALLBACK = 0
        for reason in _ORPHAN_REASONS:
            _ORPHAN[reason] = 0
            _DEAD_LETTER[reason] = 0
        for direction in _INTEGRITY_DIRECTIONS:
            for outcome in _INTEGRITY_OUTCOMES:
                _INTEGRITY[direction, outcome] = 0
        for field in _FIELD_CLASSES:
            _ENVELOPE_INVALID[field] = 0
        _PENDING_WITHOUT_DUE = 0
        _DUE_WITHOUT_PAYLOAD = 0
