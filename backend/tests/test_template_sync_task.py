from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.core.jobtrack import JobSpec
from app.services.outbox import OutboxClaim
from app.tasks import template as template_task_module
from app.tasks.template import sync_template, sync_templates


def test_template_sync_task_declares_ten_minute_heartbeat() -> None:
    assert cast(Any, sync_templates).run.job_spec == JobSpec("sync_templates", 600)


@pytest.mark.asyncio
async def test_exact_template_sync_claim_carries_one_id_to_vendor_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    vendor = object()
    observed: list[int] = []

    class VendorContext:
        async def __aenter__(self) -> object:
            return vendor

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeZhihuiClient:
        @classmethod
        def from_settings(cls, _settings: object) -> VendorContext:
            return VendorContext()

    class FakeService:
        def __init__(self, _repository: object, selected_vendor: object) -> None:
            assert selected_vendor is vendor

        async def sync_pending(self, template_id: int | None = None) -> int:
            assert template_id == 7
            observed.append(template_id)
            return 1

    class FakeExecutor:
        def __init__(self, _repository: object) -> None:
            pass

        async def run(
            self,
            selected_event_id: UUID,
            *,
            expected_type: str,
            effect: Any,
        ) -> int:
            assert selected_event_id == event_id
            assert expected_type == "template.sync"
            return await effect(OutboxClaim(event_id, uuid4(), "template.sync", (7,)))

    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    monkeypatch.setattr(template_task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(template_task_module, "SqlOutboxRepository", lambda _settings: object())
    monkeypatch.setattr(template_task_module, "SqlTemplateRepository", lambda _settings: object())
    monkeypatch.setattr(template_task_module, "OutboxExecutor", FakeExecutor)
    monkeypatch.setattr(template_task_module, "ZhihuiClient", FakeZhihuiClient)
    monkeypatch.setattr(template_task_module, "TemplateManagementService", FakeService)

    assert await template_task_module._sync_one(7, str(event_id)) == 1
    assert observed == [7]
    assert cast(Any, sync_template).name == "app.tasks.sync_template"


@pytest.mark.asyncio
async def test_exact_template_sync_rejects_mismatched_claim_before_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    vendor_constructed = False

    class FakeZhihuiClient:
        @classmethod
        def from_settings(cls, _settings: object) -> object:
            nonlocal vendor_constructed
            vendor_constructed = True
            raise AssertionError("vendor must not be constructed")

    class FakeExecutor:
        def __init__(self, _repository: object) -> None:
            pass

        async def run(
            self,
            selected_event_id: UUID,
            *,
            expected_type: str,
            effect: Any,
        ) -> int:
            assert selected_event_id == event_id
            assert expected_type == "template.sync"
            return await effect(OutboxClaim(event_id, uuid4(), "template.sync", (8,)))

    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    monkeypatch.setattr(template_task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(template_task_module, "SqlOutboxRepository", lambda _settings: object())
    monkeypatch.setattr(template_task_module, "OutboxExecutor", FakeExecutor)
    monkeypatch.setattr(template_task_module, "ZhihuiClient", FakeZhihuiClient)

    with pytest.raises(ValueError, match="args mismatch"):
        await template_task_module._sync_one(7, str(event_id))
    assert vendor_constructed is False
