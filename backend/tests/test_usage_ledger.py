from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.usage_ledger import (
    APPLY_PROJECTION_LUA,
    APPLY_PROJECTIONS_LUA,
    ProjectionRow,
    UsageLedgerService,
    UsageProjectionUnavailable,
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
    minute, minute_end, frequency_day, frequency_date, frequency_end = (
        frequency_windows(before_midnight)
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
        "acceptance:3:"
        "6f9fe86ee23dc0f44215fb19874716980fd98bf965a81ab1028ecae8d04d3628"
        ":20260726",
        "acceptance:v2:"
        "6f9fe86ee23dc0f44215fb19874716980fd98bf965a81ab1028ecae8d04d3628"
        ":20260811",
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
