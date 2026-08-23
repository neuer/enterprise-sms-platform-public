from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.services.usage_ledger import (
    APPLY_PROJECTION_LUA,
    APPLY_PROJECTIONS_LUA,
    FREQUENCY_MERGE_FUTURE_DAY_SKEW,
    FREQUENCY_MERGE_FUTURE_MINUTE_SKEW,
    ProjectionRow,
    UsageLedgerService,
    UsageProjectionUnavailable,
    UsageReservationConflict,
    _canonical_frequency_projection_key,
    _choose_frequency_merge_window,
    _frequency_projection_keys,
    _frequency_window_is_future_skewed,
    _frequency_window_sort_key,
    _latest_projection_rows,
    _ProjectionChange,
    _release_change_should_apply,
    _safe_event_id,
    _safe_request_key,
    frequency_windows,
    shanghai_day,
)


class ProjectionRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail = False

    async def get(self, name: str) -> str | None:
        if self.fail:
            raise ConnectionError
        return self.values.get(name)

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        if self.fail:
            raise ConnectionError
        if kwargs.get("nx") and name in self.values:
            return False
        self.values[name] = str(value)
        return True

    async def mget(self, keys: list[str]) -> list[str | None]:
        if self.fail:
            raise ConnectionError
        return [self.values.get(key) for key in keys]

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        if self.fail:
            raise ConnectionError
        if script == APPLY_PROJECTION_LUA:
            assert numkeys == 2
            key, version_key, value, version, _expires_at = args
            rows = [(key, version_key, value, version)]
        else:
            assert script == APPLY_PROJECTIONS_LUA and numkeys % 2 == 0
            keys = args[:numkeys]
            values = args[numkeys:]
            rows = [
                (
                    keys[index * 2],
                    keys[index * 2 + 1],
                    values[index * 3],
                    values[index * 3 + 1],
                )
                for index in range(numkeys // 2)
            ]
        applied = 0
        for key, version_key, value, version in rows:
            current = int(self.values.get(str(version_key), "-1"))
            if current > int(version):
                continue
            self.values[str(key)] = str(value)
            self.values[str(version_key)] = str(version)
            applied += 1
        return applied


def test_shanghai_boundaries_are_explicit_and_timezone_aware() -> None:
    before_midnight = datetime(2026, 7, 26, 15, 59, 59, tzinfo=UTC)
    date_key, usage_date, next_day = shanghai_day(before_midnight)
    minute, minute_end, frequency_day, frequency_date, frequency_end = frequency_windows(
        before_midnight
    )

    assert date_key == "20260726"
    assert usage_date == date(2026, 7, 26)
    assert next_day.isoformat() == "2026-07-27T00:00:00+08:00"
    assert minute == str(int(before_midnight.timestamp() // 60))
    assert minute_end.isoformat() == "2026-07-26T16:00:00+00:00"
    assert frequency_day == date_key
    assert frequency_date == usage_date
    assert frequency_end == next_day

    with pytest.raises(ValueError, match="timezone-aware"):
        shanghai_day(datetime(2026, 7, 26))


@pytest.mark.parametrize(
    "value",
    [
        "acceptance:3:6f9fe86ee23dc0f44215fb19874716980fd98bf965a81ab1028ecae8d04d3628:20260726",
        "acceptance:v2:6f9fe86ee23dc0f44215fb19874716980fd98bf965a81ab1028ecae8d04d3628:20260811",
        "acceptance:123e4567-e89b-42d3-a456-426614174000",
        "legacy:batch:cbfc7a12676741399d36bf52390f0fb1",
    ],
)
def test_usage_request_keys_accept_opaque_references_without_phone_false_positive(
    value: str,
) -> None:
    assert _safe_request_key(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "arbitrary:13800138000",
        "acceptance:3:13800138000:20260726",
        "legacy:batch:13800138000",
    ],
)
def test_usage_request_keys_reject_unstructured_or_phone_material(value: str) -> None:
    with pytest.raises(ValueError, match="request key"):
        _safe_request_key(value)


@pytest.mark.parametrize(
    "value",
    [
        "batch:cbfc7a12676741399d36bf52390f0fb1:cancelled",
        "approval:17:expired",
        "usage:123e4567-e89b-42d3-a456-426614174000:acceptance-failed",
    ],
)
def test_usage_release_ids_accept_only_structured_references(value: str) -> None:
    assert _safe_event_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "usage:123e4567-e89b-42d3-a456-426614174000:13800138000",
        "batch:13800138000:cancelled",
        "approval:17:custom",
    ],
)
def test_usage_release_ids_reject_phone_or_unknown_reason(value: str) -> None:
    with pytest.raises(ValueError, match="release event"):
        _safe_event_id(value)


@pytest.mark.asyncio
async def test_projection_absolute_write_rejects_stale_out_of_order_version() -> None:
    redis = ProjectionRedis()
    service = UsageLedgerService(redis, object())  # type: ignore[arg-type]
    expires = datetime(2026, 7, 27, tzinfo=UTC)

    await service._apply_rows(  # noqa: SLF001
        (
            ProjectionRow(
                "quota:app:7:20260726",
                "quota",
                date(2026, 7, 26),
                9,
                12,
                expires,
            ),
        )
    )
    await service._apply_rows(  # noqa: SLF001
        (
            ProjectionRow(
                "quota:app:7:20260726",
                "quota",
                date(2026, 7, 26),
                3,
                11,
                expires,
            ),
        )
    )

    assert redis.values["quota:app:7:20260726"] == "9"
    assert redis.values["usage:projection:version:quota:app:7:20260726"] == "12"


@pytest.mark.asyncio
async def test_redis_unavailable_is_fail_closed_before_reservation() -> None:
    redis = ProjectionRedis()
    redis.fail = True
    service = UsageLedgerService(redis, object())  # type: ignore[arg-type]

    with pytest.raises(UsageProjectionUnavailable):
        await service.ensure_ready(datetime(2026, 7, 26, 8, tzinfo=UTC))


def test_frequency_projection_keys_are_deterministic() -> None:
    digest = "a" * 64
    assert _frequency_projection_keys("verify", 1, digest) == (
        f"freq:v:{digest}:m",
        f"freq:v:{digest}:d",
    )
    assert _frequency_projection_keys("market", 9, digest) == (f"freq:m:9:{digest}:d",)


def test_canonical_frequency_projection_key_remaps_verify_and_market() -> None:
    source = "a" * 64
    canonical = "b" * 64
    assert (
        _canonical_frequency_projection_key(f"freq:v:{source}:m", canonical)
        == f"freq:v:{canonical}:m"
    )
    assert (
        _canonical_frequency_projection_key(f"freq:v:{source}:d", canonical)
        == f"freq:v:{canonical}:d"
    )
    assert (
        _canonical_frequency_projection_key(f"freq:m:12:{source}:d", canonical)
        == f"freq:m:12:{canonical}:d"
    )
    assert (
        _canonical_frequency_projection_key(f"freq:v:{canonical}:m", canonical)
        == f"freq:v:{canonical}:m"
    )
    assert _canonical_frequency_projection_key("quota:app:1:20260822", canonical) is None


def _frequency_row(
    dimension_key: str,
    *,
    usage_date: date,
    window_key: str,
    value: int,
    expires_at: datetime,
) -> dict[str, Any]:
    return {
        "dimension_key": dimension_key,
        "kind": "frequency",
        "usage_date": usage_date,
        "window_key": window_key,
        "expires_at": expires_at,
        "value": value,
    }


def test_choose_frequency_merge_window_prefers_canonical_row() -> None:
    target = "freq:v:" + "b" * 64 + ":m"
    source = _frequency_row(
        "freq:v:" + "a" * 64 + ":m",
        usage_date=date(2026, 8, 21),
        window_key="111",
        value=3,
        expires_at=datetime(2026, 8, 21, 16, tzinfo=UTC),
    )
    canonical = _frequency_row(
        target,
        usage_date=date(2026, 8, 22),
        window_key="222",
        value=1,
        expires_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
    )
    chosen = _choose_frequency_merge_window((source, canonical), target_key=target)
    assert chosen is not None
    kind, usage_date, window_key, expires_at, matching = chosen
    assert (kind, usage_date, window_key) == ("frequency", date(2026, 8, 22), "222")
    assert expires_at == canonical["expires_at"]
    assert matching == (canonical,)
    assert _choose_frequency_merge_window((), target_key=target) is None


@pytest.mark.parametrize(
    ("target", "source_key"),
    [
        ("freq:v:" + "b" * 64 + ":m", "freq:v:" + "a" * 64 + ":m"),
        ("freq:v:" + "b" * 64 + ":d", "freq:v:" + "a" * 64 + ":d"),
        ("freq:m:9:" + "b" * 64 + ":d", "freq:m:9:" + "a" * 64 + ":d"),
    ],
)
def test_choose_frequency_merge_window_keeps_newest_over_old_canonical(
    target: str,
    source_key: str,
) -> None:
    source = _frequency_row(
        source_key,
        usage_date=date(2026, 8, 22),
        window_key="222",
        value=5,
        expires_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
    )
    canonical = _frequency_row(
        target,
        usage_date=date(2026, 8, 21),
        window_key="111",
        value=3,
        expires_at=datetime(2026, 8, 22, 2, tzinfo=UTC),
    )
    chosen = _choose_frequency_merge_window((canonical, source), target_key=target)
    assert chosen is not None
    kind, usage_date, window_key, expires_at, matching = chosen
    assert (kind, usage_date, window_key) == ("frequency", date(2026, 8, 22), "222")
    assert expires_at == source["expires_at"]
    assert matching == (source,)


def test_choose_frequency_merge_window_same_newest_window_prefers_canonical() -> None:
    target = "freq:v:" + "b" * 64 + ":m"
    source = _frequency_row(
        "freq:v:" + "a" * 64 + ":m",
        usage_date=date(2026, 8, 22),
        window_key="222",
        value=4,
        expires_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
    )
    canonical = _frequency_row(
        target,
        usage_date=date(2026, 8, 22),
        window_key="222",
        value=2,
        expires_at=datetime(2026, 8, 22, 0, 30, tzinfo=UTC),
    )
    chosen = _choose_frequency_merge_window((source, canonical), target_key=target)
    assert chosen is not None
    kind, usage_date, window_key, expires_at, matching = chosen
    assert (kind, usage_date, window_key) == ("frequency", date(2026, 8, 22), "222")
    assert expires_at == source["expires_at"]
    assert matching == (source, canonical)
    assert sum(int(row["value"]) for row in matching) == 6


def test_choose_frequency_merge_window_orders_numeric_minute_keys() -> None:
    target = "freq:v:" + "b" * 64 + ":m"
    source = _frequency_row(
        "freq:v:" + "a" * 64 + ":m",
        usage_date=date(2026, 8, 22),
        window_key="10",
        value=1,
        expires_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
    )
    canonical = _frequency_row(
        target,
        usage_date=date(2026, 8, 22),
        window_key="9",
        value=7,
        expires_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
    )
    assert _frequency_window_sort_key(source) > _frequency_window_sort_key(canonical)
    chosen = _choose_frequency_merge_window((canonical, source), target_key=target)
    assert chosen is not None
    assert chosen[2] == "10"
    assert chosen[4] == (source,)


def test_frequency_window_future_skew_allows_boundary_but_rejects_far_future() -> None:
    observed = datetime(2026, 8, 22, 4, 10, tzinfo=UTC)
    minute, _, date_key, usage_date, _ = frequency_windows(observed)
    minute_key = "freq:v:" + "b" * 64 + ":m"
    day_key = "freq:v:" + "b" * 64 + ":d"
    market_key = "freq:m:3:" + "b" * 64 + ":d"

    assert not _frequency_window_is_future_skewed(
        dimension_key=minute_key,
        usage_date=usage_date,
        window_key=minute,
        observed=observed,
    )
    assert not _frequency_window_is_future_skewed(
        dimension_key=minute_key,
        usage_date=usage_date,
        window_key=str(int(minute) + FREQUENCY_MERGE_FUTURE_MINUTE_SKEW),
        observed=observed,
    )
    assert _frequency_window_is_future_skewed(
        dimension_key=minute_key,
        usage_date=usage_date,
        window_key=str(int(minute) + FREQUENCY_MERGE_FUTURE_MINUTE_SKEW + 1),
        observed=observed,
    )
    assert _frequency_window_is_future_skewed(
        dimension_key=minute_key,
        usage_date=usage_date,
        window_key="not-a-minute",
        observed=observed,
    )
    assert not _frequency_window_is_future_skewed(
        dimension_key=day_key,
        usage_date=usage_date + timedelta(days=FREQUENCY_MERGE_FUTURE_DAY_SKEW),
        window_key=date_key,
        observed=observed,
    )
    assert _frequency_window_is_future_skewed(
        dimension_key=market_key,
        usage_date=usage_date + timedelta(days=FREQUENCY_MERGE_FUTURE_DAY_SKEW + 1),
        window_key=date_key,
        observed=observed,
    )


def test_release_change_skips_only_expired_missing_frequency() -> None:
    observed = datetime(2026, 8, 22, 12, tzinfo=UTC)
    expired = datetime(2026, 8, 22, 11, tzinfo=UTC)
    live = datetime(2026, 8, 22, 13, tzinfo=UTC)
    freq_key = "freq:v:" + "a" * 64 + ":m"
    expired_freq = _ProjectionChange(
        dimension_key=freq_key,
        kind="frequency",
        usage_date=date(2026, 8, 22),
        window_key="123",
        delta=-1,
        expires_at=expired,
        reset_on_window_change=False,
    )
    live_freq = _ProjectionChange(
        dimension_key=freq_key,
        kind="frequency",
        usage_date=date(2026, 8, 22),
        window_key="124",
        delta=-1,
        expires_at=live,
        reset_on_window_change=False,
    )
    expired_quota = _ProjectionChange(
        dimension_key="quota:app:1:20260822",
        kind="quota",
        usage_date=date(2026, 8, 22),
        window_key="20260822",
        delta=-1,
        expires_at=expired,
        reset_on_window_change=False,
    )
    assert _release_change_should_apply(
        expired_freq,
        existing_keys={freq_key},
        observed=observed,
    )
    assert not _release_change_should_apply(
        expired_freq,
        existing_keys=set(),
        observed=observed,
    )
    with pytest.raises(UsageReservationConflict, match="projection batch update conflict"):
        _release_change_should_apply(live_freq, existing_keys=set(), observed=observed)
    with pytest.raises(UsageReservationConflict, match="projection batch update conflict"):
        _release_change_should_apply(expired_quota, existing_keys=set(), observed=observed)


def test_latest_projection_rows_keep_higher_version_per_key() -> None:
    expires = datetime(2026, 8, 23, tzinfo=UTC)
    key = "freq:v:" + "a" * 64
    older = ProjectionRow(f"{key}:m", "frequency", date(2026, 8, 22), 1, 3, expires)
    newer = ProjectionRow(f"{key}:m", "frequency", date(2026, 8, 22), 2, 9, expires)
    other = ProjectionRow(f"{key}:d", "frequency", date(2026, 8, 22), 4, 4, expires)
    assert _latest_projection_rows((older, other, newer)) == (other, newer)
