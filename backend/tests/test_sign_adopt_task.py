from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.services.outbox import OutboxClaim
from app.tasks import sign as sign_task_module
from app.tasks.sign import adopt_sign


@pytest.mark.asyncio
async def test_exact_sign_adoption_claim_reaches_vendor_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    vendor = object()
    observed: list[tuple[int, int]] = []

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

        async def adopt_existing(self, sign_id: int, vendor_sign_id: int) -> int:
            observed.append((sign_id, vendor_sign_id))
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
            assert expected_type == "sign.adopt"
            return await effect(
                OutboxClaim(event_id, uuid4(), "sign.adopt", (1, 112074))
            )

    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    monkeypatch.setattr(sign_task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sign_task_module, "SqlOutboxRepository", lambda _settings: object())
    monkeypatch.setattr(sign_task_module, "SqlSignRepository", lambda _settings: object())
    monkeypatch.setattr(sign_task_module, "OutboxExecutor", FakeExecutor)
    monkeypatch.setattr(sign_task_module, "ZhihuiClient", FakeZhihuiClient)
    monkeypatch.setattr(sign_task_module, "SignManagementService", FakeService)

    assert await sign_task_module._adopt(1, 112074, str(event_id)) == 1
    assert observed == [(1, 112074)]
    assert cast(Any, adopt_sign).name == "app.tasks.adopt_sign"


@pytest.mark.asyncio
async def test_sign_adoption_rejects_mismatched_claim_before_vendor(
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
            assert expected_type == "sign.adopt"
            return await effect(
                OutboxClaim(event_id, uuid4(), "sign.adopt", (1, 112075))
            )

    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    monkeypatch.setattr(sign_task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sign_task_module, "SqlOutboxRepository", lambda _settings: object())
    monkeypatch.setattr(sign_task_module, "OutboxExecutor", FakeExecutor)
    monkeypatch.setattr(sign_task_module, "ZhihuiClient", FakeZhihuiClient)

    with pytest.raises(ValueError, match="args mismatch"):
        await sign_task_module._adopt(1, 112074, str(event_id))
    assert vendor_constructed is False
