from __future__ import annotations

from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal, UncertainEffectPrincipal
from app.services.outbox import OutboxEventSpec
from app.services.uncertain_resolution import (
    UncertainResolutionConflict,
    UncertainResolutionService,
    _apply_not_accepted,
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

    def scalar_one_or_none(self) -> object:
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


def _resolution(
    *,
    state: str = "proposed",
    action: str = "keep_unknown",
    confirmer: int | None = None,
) -> dict[str, object]:
    return {
        "id": 4,
        "chunk_id": 9,
        "batch_id": 3,
        "action": action,
        "state": state,
        "proposer_account_id": 1,
        "confirmer_account_id": confirmer,
        "child_batch_id": None,
        "effect_generation": 1,
        "effect_error": None,
        "app_id": 7,
        "channel": "api",
        "category": "notice",
        "source_app_id": 7,
        "source_channel": "api",
        "source_category": "notice",
    }


@pytest.mark.asyncio
async def test_same_admin_cannot_confirm_own_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([FakeResult(_resolution())])

    with pytest.raises(UncertainResolutionConflict, match="确认人不能是提案人"):
        await service(monkeypatch, connection).confirm(4, PROPOSER)


@pytest.mark.asyncio
async def test_second_admin_confirm_only_enqueues_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.uncertain_resolution as module

    enqueued: list[OutboxEventSpec] = []

    async def fake_enqueue(_connection: object, spec: OutboxEventSpec) -> None:
        enqueued.append(spec)

    released: list[object] = []

    async def fake_release(*_args: object, **_kwargs: object) -> bool:
        released.append(_kwargs)
        return True

    monkeypatch.setattr(module, "enqueue_outbox", fake_enqueue)
    monkeypatch.setattr(module, "request_usage_release_for_batch", fake_release)
    pending = {**_resolution(), "state": "effect_pending", "confirmer_account_id": 2}
    connection = FakeConnection(
        [
            FakeResult(_resolution()),
            FakeResult(pending),
        ]
    )

    item = await service(monkeypatch, connection).confirm(4, CONFIRMER)

    assert item.state == "effect_pending"
    assert item.confirmer_account_id == 2
    assert released == []
    assert len(enqueued) == 1
    assert enqueued[0].task_name == "app.tasks.outbox.apply_uncertain_effect"
    assert enqueued[0].dedup_key == "uncertain.effect:4:1"
    sql = "\n".join(call[0] for call in connection.calls)
    assert "effect_pending" in sql
    assert "SET status='pending'" not in sql


@pytest.mark.asyncio
async def test_propose_requires_unknown_terminal_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult({"id": 9, "batch_id": 3}),
            FakeResult(_resolution(action="confirm_accepted")),
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
        [FakeResult(_resolution(state="effect_pending", confirmer=2))]
    )

    with pytest.raises(UncertainResolutionConflict, match="处置单已确认"):
        await service(monkeypatch, connection).confirm(4, CONFIRMER)


@pytest.mark.asyncio
async def test_not_accepted_release_is_chunk_fact_and_batch_only_when_all_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unused = FakeConnection(
        [
            FakeResult(
                {
                    "reservation_id": "11111111-1111-1111-1111-111111111111",
                    "recipient_count": 10,
                    "segment_count": 10,
                    "request_count": 1,
                }
            ),
            FakeResult(),
            FakeResult(scalar=False),
            FakeResult(scalar="11111111-1111-1111-1111-111111111111"),
        ]
    )
    released: list[tuple[int, str]] = []

    async def fake_release(
        _connection: object,
        *,
        batch_id: int,
        event_id: str,
    ) -> bool:
        released.append((batch_id, event_id))
        return True

    import app.services.uncertain_resolution as module

    monkeypatch.setattr(module, "request_usage_release_for_batch", fake_release)
    await _apply_not_accepted(
        unused,  # type: ignore[arg-type]
        resolution_id=4,
        chunk_id=9,
        batch_id=3,
    )
    assert released == [
        (3, "usage:11111111-1111-1111-1111-111111111111:uncertain-unused")
    ]
    assert unused.calls[1][1]["event_id"] == "resolution:4:not-accepted"

    released.clear()
    leftover = FakeConnection(
        [
            FakeResult(
                {
                    "reservation_id": "11111111-1111-1111-1111-111111111111",
                    "recipient_count": 10,
                    "segment_count": 10,
                    "request_count": 0,
                }
            ),
            FakeResult(),
            FakeResult(scalar=True),
        ]
    )
    await _apply_not_accepted(
        leftover,  # type: ignore[arg-type]
        resolution_id=5,
        chunk_id=10,
        batch_id=3,
    )
    assert released == []


@pytest.mark.asyncio
async def test_resend_builds_resolution_scoped_biz_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCrypto:
        def decrypt_phone(self, *_: object) -> str:
            return "13800138000"

        def decrypt_bound_packed_text(self, *_: object) -> str:
            return "通知内容"

    connection = FakeConnection(
        [
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
    request = await item._build_resend(
        connection,
        chunk_id=9,
        resolution_id=4,
        generation=2,
        actor=UncertainEffectPrincipal(
            resolution_id=4,
            proposer_account_id=1,
            confirmer_account_id=2,
            effect_generation=2,
            dept="平台部",
        ),
    )
    assert request.biz_id == "manual-resend:4:2"
    assert request.resend_of is None
    assert request.mobiles == ("13800138000",)
    assert isinstance(request.actor, UncertainEffectPrincipal)
    assert request.actor.confirmer_account_id == 2
    sql = "\n".join(call[0] for call in connection.calls)
    assert "SET status='pending'" not in sql
    assert "unknown_terminal" in sql
