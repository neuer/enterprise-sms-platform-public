from __future__ import annotations

from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.uncertain_resolution import (
    UncertainResolutionConflict,
    UncertainResolutionService,
)

PROPOSER = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")
CONFIRMER = SecurityPrincipal(2, 20, "admin02", "平台部", "admin")


class FakeResult:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        scalar: object = None,
        rowcount: int = 0,
    ) -> None:
        self.row = row
        self.scalar = scalar
        self.rowcount = rowcount

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row

    def one(self) -> dict[str, object]:
        assert self.row is not None
        return self.row

    def scalar_one(self) -> object:
        return self.scalar

    def __iter__(self) -> Any:
        return iter([self.row] if self.row is not None else [])


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def service(
    monkeypatch: pytest.MonkeyPatch, connection: FakeConnection
) -> UncertainResolutionService:
    item = UncertainResolutionService(object())  # type: ignore[arg-type]
    monkeypatch.setattr(item, "_engine", lambda: FakeEngine(connection))
    return item


@pytest.mark.asyncio
async def test_same_admin_cannot_confirm_own_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(
                {
                    "id": 4,
                    "chunk_id": 9,
                    "batch_id": 3,
                    "action": "keep_unknown",
                    "state": "proposed",
                    "proposer_account_id": 1,
                    "confirmer_account_id": None,
                    "child_batch_id": None,
                }
            )
        ]
    )

    with pytest.raises(UncertainResolutionConflict, match="确认人不能是提案人"):
        await service(monkeypatch, connection).confirm(4, PROPOSER)


@pytest.mark.asyncio
async def test_second_admin_confirms_keep_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirmed = {
        "id": 4,
        "chunk_id": 9,
        "batch_id": 3,
        "action": "keep_unknown",
        "state": "confirmed",
        "proposer_account_id": 1,
        "confirmer_account_id": 2,
        "child_batch_id": None,
    }
    connection = FakeConnection(
        [
            FakeResult(
                {
                    **confirmed,
                    "state": "proposed",
                    "confirmer_account_id": None,
                }
            ),
            FakeResult(confirmed),
        ]
    )

    item, resend = await service(monkeypatch, connection).confirm(4, CONFIRMER)

    assert resend is None
    assert item.state == "confirmed"
    assert item.confirmer_account_id == 2
    assert "proposer_account_id <> confirmer" not in connection.calls[1][0]
    assert connection.calls[1][1] == {"id": 4, "account_id": 2}


@pytest.mark.asyncio
async def test_propose_requires_unknown_terminal_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult({"id": 9, "batch_id": 3}),
            FakeResult(
                {
                    "id": 1,
                    "chunk_id": 9,
                    "batch_id": 3,
                    "action": "confirm_accepted",
                    "state": "proposed",
                    "proposer_account_id": 1,
                    "confirmer_account_id": None,
                    "child_batch_id": None,
                }
            ),
        ]
    )

    item = await service(monkeypatch, connection).propose(9, "confirm_accepted", PROPOSER)

    assert "unknown_terminal" in connection.calls[0][0]
    assert item.action == "confirm_accepted"
    assert "pending" not in connection.calls[0][0]


@pytest.mark.asyncio
async def test_second_propose_on_same_chunk_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult({"id": 9, "batch_id": 3}),
            FakeResult(),
        ]
    )

    with pytest.raises(UncertainResolutionConflict, match="该分片已有处置单"):
        await service(monkeypatch, connection).propose(9, "keep_unknown", PROPOSER)


@pytest.mark.asyncio
async def test_concurrent_confirm_is_rejected_after_first_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(
                {
                    "id": 4,
                    "chunk_id": 9,
                    "batch_id": 3,
                    "action": "keep_unknown",
                    "state": "confirmed",
                    "proposer_account_id": 1,
                    "confirmer_account_id": 2,
                    "child_batch_id": None,
                }
            )
        ]
    )

    with pytest.raises(UncertainResolutionConflict, match="处置单已确认"):
        await service(monkeypatch, connection).confirm(4, CONFIRMER)


@pytest.mark.asyncio
async def test_confirm_not_accepted_releases_only_when_batch_unused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.uncertain_resolution as module

    released: list[tuple[int, str]] = []

    async def fake_release(
        _connection: object,
        *,
        batch_id: int,
        event_id: str,
    ) -> bool:
        released.append((batch_id, event_id))
        return True

    monkeypatch.setattr(module, "request_usage_release_for_batch", fake_release)
    confirmed = {
        "id": 4,
        "chunk_id": 9,
        "batch_id": 3,
        "action": "confirm_not_accepted",
        "state": "confirmed",
        "proposer_account_id": 1,
        "confirmer_account_id": 2,
        "child_batch_id": None,
    }
    unused = FakeConnection(
        [
            FakeResult({**confirmed, "state": "proposed", "confirmer_account_id": None}),
            FakeResult(confirmed),
            FakeResult(scalar=True),
        ]
    )
    await service(monkeypatch, unused).confirm(4, CONFIRMER)
    assert released == [(3, "uncertain-unused:4")]

    released.clear()
    used = FakeConnection(
        [
            FakeResult({**confirmed, "state": "proposed", "confirmer_account_id": None}),
            FakeResult(confirmed),
            FakeResult(scalar=False),
        ]
    )
    await service(monkeypatch, used).confirm(4, CONFIRMER)
    assert released == []


@pytest.mark.asyncio
async def test_resend_builds_new_batch_identity_and_never_reopens_old_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCrypto:
        def decrypt_phone(self, *_: object) -> str:
            return "13800138000"

        def decrypt_bound_packed_text(self, *_: object) -> str:
            return "通知内容"

    confirmed = {
        "id": 4,
        "chunk_id": 9,
        "batch_id": 3,
        "action": "resend_new_batch",
        "state": "confirmed",
        "proposer_account_id": 1,
        "confirmer_account_id": 2,
        "child_batch_id": None,
    }
    connection = FakeConnection(
        [
            FakeResult({**confirmed, "state": "proposed", "confirmer_account_id": None}),
            FakeResult(confirmed),
            FakeResult(
                {
                    "batch_no": "BATCH-1",
                    "category": "notice",
                    "channel": "api",
                    "dept": "平台部",
                    "send_content_enc": b"cipher",
                    "sign_name": "青鸾",
                    "consent_confirmed": False,
                    "is_test": False,
                }
            ),
            FakeResult(
                {
                    "phone_enc": b"enc",
                    "phone_hmac": "a" * 64,
                    "key_version": 1,
                }
            ),
        ]
    )
    item = UncertainResolutionService(FakeCrypto())  # type: ignore[arg-type]
    monkeypatch.setattr(item, "_engine", lambda: FakeEngine(connection))

    resolution, request = await item.confirm(4, CONFIRMER)

    assert resolution.state == "confirmed"
    assert request is not None
    assert request.biz_id.startswith("unknown-recipients-v1:")
    assert request.resend_of == "BATCH-1"
    assert request.mobiles == ("13800138000",)
    sql = "\n".join(call[0] for call in connection.calls)
    assert "SET status='pending'" not in sql
    assert "unknown_terminal" in sql
