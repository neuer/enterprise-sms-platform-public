from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.category import coerce_market_dispatch, queue_for_category

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_queue_for_category_routes_market_to_bulk() -> None:
    assert queue_for_category("market") == "bulk"
    assert queue_for_category("notice") == "realtime"
    assert queue_for_category("verify") == "realtime"


def test_coerce_market_dispatch_queues_inside_window() -> None:
    now = datetime(2026, 7, 12, 10, 0, tzinfo=SHANGHAI)

    status, deferred, scheduled_at = coerce_market_dispatch(now, "08:00-21:00", None)

    assert status == "queued"
    assert deferred is None
    assert scheduled_at is None


def test_coerce_market_dispatch_defers_outside_window_and_explicit_schedule() -> None:
    now = datetime(2026, 7, 12, 22, 0, tzinfo=SHANGHAI)
    next_start = datetime(2026, 7, 13, 8, 0, tzinfo=SHANGHAI)

    status, deferred, scheduled_at = coerce_market_dispatch(now, "08:00-21:00", None)
    assert status == "scheduled"
    assert deferred == "market_window"
    assert scheduled_at == next_start

    explicit = datetime(2026, 7, 12, 22, 30, tzinfo=SHANGHAI)
    status, deferred, scheduled_at = coerce_market_dispatch(now, "08:00-21:00", explicit)
    assert status == "scheduled"
    assert deferred == "market_window"
    assert scheduled_at == next_start

    inside = datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI)
    status, deferred, scheduled_at = coerce_market_dispatch(now, "08:00-21:00", inside)
    assert status == "scheduled"
    assert deferred is None
    assert scheduled_at == inside


def test_coerce_market_dispatch_rejects_naive_datetimes() -> None:
    naive = datetime(2026, 7, 12, 10, 0)
    aware = datetime(2026, 7, 12, 10, 0, tzinfo=SHANGHAI)

    with pytest.raises(ValueError, match="timezone"):
        coerce_market_dispatch(naive, "08:00-21:00", None)
    with pytest.raises(ValueError, match="timezone"):
        coerce_market_dispatch(aware, "08:00-21:00", naive)


def test_coerce_market_dispatch_rejects_overnight_window() -> None:
    now = datetime(2026, 7, 12, 10, 0, tzinfo=SHANGHAI)
    with pytest.raises(ValueError, match="same-day"):
        coerce_market_dispatch(now, "22:00-06:00", None)
