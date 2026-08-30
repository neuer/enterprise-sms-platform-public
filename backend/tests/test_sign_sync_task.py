from typing import Any, cast

import pytest

from app.core.jobtrack import JobSpec
from app.tasks import sign as sign_task_module
from app.tasks.sign import sync_signs


def test_sign_sync_task_declares_ten_minute_heartbeat() -> None:
    assert cast(Any, sync_signs).run.job_spec == JobSpec("sync_signs", 600)


@pytest.mark.asyncio
@pytest.mark.parametrize("sign_id", [None, 7])
async def test_sign_sync_task_preserves_full_and_exact_modes(
    sign_id: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int | None] = []
    vendor = object()

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

        async def sync_pending(self, selected_sign_id: int | None = None) -> int:
            observed.append(selected_sign_id)
            return 1

    settings = type("SettingsStub", (), {"database_url": "postgresql://test"})()
    monkeypatch.setattr(sign_task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sign_task_module, "SqlSignRepository", lambda _settings: object())
    monkeypatch.setattr(sign_task_module, "ZhihuiClient", FakeZhihuiClient)
    monkeypatch.setattr(sign_task_module, "SignManagementService", FakeService)

    assert await sign_task_module._sync(sign_id) == 1
    assert observed == [sign_id]


@pytest.mark.asyncio
async def test_sign_sync_task_rejects_out_of_range_exact_id_before_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor_constructed = False

    class FakeZhihuiClient:
        @classmethod
        def from_settings(cls, _settings: object) -> object:
            nonlocal vendor_constructed
            vendor_constructed = True
            raise AssertionError("vendor must not be constructed")

    monkeypatch.setattr(sign_task_module, "ZhihuiClient", FakeZhihuiClient)

    with pytest.raises(ValueError, match="invalid sign sync id"):
        await sign_task_module._sync(2_147_483_648)
    assert vendor_constructed is False
