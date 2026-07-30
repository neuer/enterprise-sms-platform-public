from __future__ import annotations

import pytest


class FakeRecipientRepository:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[tuple[str, str]] = []

    async def purge_all(self, *, actor: str) -> int:
        self.calls.append(("purge_all", actor))
        return self.count


@pytest.mark.asyncio
async def test_reset_finalizer_purges_by_operation_actor_without_public_result() -> None:
    from datetime import UTC, datetime

    from app.services.vendor_test_operation import VendorTestOperation
    from app.services.vendor_test_reset import VendorTestResetFinalizer

    repository = FakeRecipientRepository(3)
    record = VendorTestOperation(
        operation_id="c0a80101-0000-4000-8000-000000000081",
        operation_type="reset_configuration",
        actor="admin",
        status="running",
        safe_code=None,
        batch_no=None,
        checkpoint_id=None,
        requested_at=datetime(2026, 7, 17, 9, tzinfo=UTC),
        completed_at=None,
    )

    result = await VendorTestResetFinalizer(repository).finalize(record)

    assert result is None
    assert repository.calls == [("purge_all", "admin")]
    assert "3" not in repr(result)
