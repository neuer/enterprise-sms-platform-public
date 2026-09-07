from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from app.core.jobtrack import JOB_SPECS, JobSpec
from app.tasks.scheduler import build_beat_schedule


def test_housekeeping_task_is_registered_tracked_and_fixed_to_bulk_queue() -> None:
    from app.tasks.housekeeping import housekeeping

    assert cast(Any, housekeeping).name == "app.tasks.housekeeping"
    assert JOB_SPECS["housekeeping"] == JobSpec("housekeeping", 86400)
    assert build_beat_schedule({})["housekeeping"] == {
        "task": "app.tasks.housekeeping",
        "schedule": 86400,
        "options": {"queue": "bulk"},
    }


async def test_housekeeping_queues_next_bounded_round_when_work_remains(
    monkeypatch: Any,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from app.services.housekeeping import CleanupCounts
    from app.tasks import housekeeping as task_module

    settings = Mock()
    settings.import_storage_dir = Path("/tmp")
    service = Mock()
    service.run = AsyncMock(return_value=CleanupCounts(imports=32, has_more=True))
    monkeypatch.setattr(task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(task_module, "SqlHousekeepingRepository", lambda _: object())
    monkeypatch.setattr(task_module, "HousekeepingService", lambda *_: service)
    send = Mock()
    monkeypatch.setattr(task_module.celery_app, "send_task", send)

    assert await task_module._run() == 32
    send.assert_called_once_with(
        "app.tasks.housekeeping",
        queue="bulk",
        countdown=30,
        ignore_result=True,
    )
    send.reset_mock()
    service.run.return_value = CleanupCounts()
    assert await task_module._run() == 0
    send.assert_not_called()
