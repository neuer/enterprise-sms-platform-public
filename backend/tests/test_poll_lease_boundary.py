"""轮询租约失败只在安全点退出，拉走即消费响应必须先捕获。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.redis_lock import LeaseUncertain
from app.services.raw_spill import RawSpillStore
from app.services.reply_ingest import ReplyIngestService
from app.services.report_ingest import ReportIngestService
from tests.test_raw_spill_artifact_state import (
    FakeAlerts,
    FakeRepository,
    RecordingGateway,
    crypto,
    empty_payload,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", [ReportIngestService, ReplyIngestService])
@pytest.mark.parametrize("fail_at", [1, 2])
async def test_poll_checks_before_pull_and_after_persistence(
    tmp_path: Path, service_type: type, fail_at: int
) -> None:
    gateway = RecordingGateway(empty_payload())
    repository = FakeRepository()
    store = RawSpillStore(tmp_path)
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise LeaseUncertain("synthetic loss")

    service = service_type(gateway, repository, crypto(), alerts=FakeAlerts(), spill=store)
    with pytest.raises(LeaseUncertain):
        await service.poll_once(check_lease=check)
    assert gateway.calls == fail_at - 1
    persisted = [event for event in repository.events if event[0] == "persist_raw"]
    assert len(persisted) == fail_at - 1
    if persisted:
        assert persisted[0][1]["payload_enc"] != gateway.result.raw_payload
        assert not any(event[0] == "processed" for event in repository.events)
