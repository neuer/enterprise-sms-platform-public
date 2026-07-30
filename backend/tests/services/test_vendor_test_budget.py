from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.vendor_test_budget import (
    SubmissionClaim,
    SubmissionClaimStatus,
    live_test_usage_window,
    settle_live_test_attempt,
)


class FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class FakeConnection:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return FakeResult(self.rowcount)


def test_live_test_usage_window_uses_shanghai_natural_day() -> None:
    usage_date, reset_at = live_test_usage_window(
        datetime(2026, 7, 16, 15, 59, 59, tzinfo=UTC)
    )

    assert usage_date.isoformat() == "2026-07-16"
    assert reset_at.isoformat() == "2026-07-17T00:00:00+08:00"
    assert SubmissionClaim(SubmissionClaimStatus.CLAIMED).reset_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "counter"),
    [("confirmed", "confirmed_segments"), ("uncertain", "uncertain_segments")],
)
async def test_settlement_moves_reserved_segments_atomically(
    status: str,
    counter: str,
) -> None:
    connection = FakeConnection()

    changed = await settle_live_test_attempt(connection, 7, status)  # type: ignore[arg-type]

    assert changed == 1
    sql, params = connection.calls[0]
    assert "status='reserved'" in sql
    assert "in_flight_segments=u.in_flight_segments-settled.segments" in sql
    assert f"{counter}=u.{counter}+settled.segments" in sql
    assert params == {"chunk_id": 7, "status": status}


@pytest.mark.asyncio
async def test_explicit_rejection_releases_reservation_without_consuming_budget() -> None:
    connection = FakeConnection()

    changed = await settle_live_test_attempt(connection, 7, "released")  # type: ignore[arg-type]

    assert changed == 1
    sql, params = connection.calls[0]
    assert "SET status=:status" in sql
    assert "confirmed_segments=u.confirmed_segments" in sql
    assert "uncertain_segments=u.uncertain_segments" in sql
    assert params == {"chunk_id": 7, "status": "released"}
