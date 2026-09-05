from __future__ import annotations

from typing import Any

import pytest
from app.core.auth.accounts import SecurityPrincipal, UncertainEffectPrincipal
from app.services.outbox import OutboxEventSpec
from app.services.pipeline import SendRequest
from app.services.uncertain_resolution import (
    UncertainResolutionConflict,
    UncertainResolutionService,
    _apply_not_accepted,
    _load_resend_context,
    _row,
)
from app.services.usage_subject import UsageSubject

PROPOSER = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")
CONFIRMER = SecurityPrincipal(2, 20, "admin02", "平台部", "admin")


class FakeResult:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        scalar: object = None,
        rowcount: int = 0,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.row = row
        self.scalar = scalar
        self.rowcount = rowcount
        self.rows = rows

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
        if self.rows is not None:
            return iter(self.rows)
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
        "source_dept": "平台部",
        "dept": "平台部",
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
    assert "source_dept" in sql
    assert connection.calls[1][1]["dept"] == "平台部"
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
    usage = UsageSubject(
        kind="system_effect",
        app_id=88,
        dept="运营一部",
        category="notice",
        resolution_id=4,
        effect_generation=2,
    )
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
            dept="运营一部",
        ),
        usage_subject=usage,
    )
    assert request.biz_id == "manual-resend:4:2"
    assert request.resend_of is None
    assert request.mobiles == ("13800138000",)
    assert isinstance(request.actor, UncertainEffectPrincipal)
    assert request.actor.confirmer_account_id == 2
    assert request.actor.actor_account_id is None
    assert request.usage_subject == usage
    assert request.usage_subject.app_id != -1
    sql = "\n".join(call[0] for call in connection.calls)
    assert "SET status='pending'" not in sql
    assert "unknown_terminal" in sql


@pytest.mark.asyncio
async def test_confirm_without_source_dept_is_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([FakeResult({**_resolution(), "dept": ""})])

    with pytest.raises(UncertainResolutionConflict, match="source dept unavailable"):
        await service(monkeypatch, connection).confirm(4, CONFIRMER)


@pytest.mark.asyncio
async def test_web_resend_context_uses_system_app_and_source_dept() -> None:
    current = _row(
        {
            **_resolution(
                state="applying",
                action="resend_new_batch",
                confirmer=2,
            ),
            "source_app_id": None,
            "source_channel": "web",
            "source_dept": "运营一部",
        }
    )
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {"id": 1, "status": 1, "role": "admin"},
                    {"id": 2, "status": 1, "role": "admin"},
                ]
            ),
            FakeResult(
                {
                    "id": 88,
                    "name": "system-uncertain-resend",
                    "daily_quota": 10000,
                    "allowed_categories": "verify,notice,market",
                    "max_in_flight_chunks": 200,
                    "rate_limit_per_min": 60,
                    "blacklist_check": True,
                }
            ),
        ]
    )

    context, app_ctx = await _load_resend_context(connection, current)

    assert context.usage_subject.kind == "system_effect"
    assert context.usage_subject.app_id == 88
    assert context.usage_subject.dept == "运营一部"
    assert context.source_dept == "运营一部"
    assert app_ctx.app_id == 88
    assert app_ctx.dept == "运营一部"
    assert app_ctx.name == "system-uncertain-resend"


@pytest.mark.asyncio
async def test_api_resend_context_rejects_disabled_source_app() -> None:
    current = _row(
        {
            **_resolution(
                state="applying",
                action="resend_new_batch",
                confirmer=2,
            ),
            "source_channel": "api",
        }
    )
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {"id": 1, "status": 1, "role": "admin"},
                    {"id": 2, "status": 1, "role": "admin"},
                ]
            ),
            FakeResult(
                {
                    "id": 7,
                    "name": "oa",
                    "dept": "平台部",
                    "allowed_categories": "notice",
                    "daily_quota": 100,
                    "status": 0,
                    "unlimited_quota_exempt_until": None,
                    "max_in_flight_chunks": 200,
                    "rate_limit_per_min": 60,
                    "blacklist_check": True,
                }
            ),
        ]
    )

    with pytest.raises(UncertainResolutionConflict, match="源应用不可用"):
        await _load_resend_context(connection, current)


def test_usage_subject_rejects_negative_app_id() -> None:
    with pytest.raises(ValueError, match="positive id"):
        UsageSubject(
            kind="system_effect",
            app_id=-1,
            dept="运营一部",
            category="notice",
            resolution_id=4,
            effect_generation=1,
        )


def test_pipeline_usage_subject_is_not_forgeable_from_http() -> None:
    from app.services.pipeline import SendPipeline

    usage = UsageSubject(
        kind="system_effect",
        app_id=88,
        dept="运营一部",
        category="notice",
        resolution_id=4,
        effect_generation=1,
    )
    with pytest.raises(ValueError, match="not forgeable"):
        SendPipeline._validate_usage_subject(
            SendRequest("notice", ["13800138000"], content="通知", usage_subject=usage)
        )
    with pytest.raises(ValueError, match="system resend requires usage subject"):
        SendPipeline._validate_usage_subject(
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                actor=UncertainEffectPrincipal(
                    resolution_id=4,
                    proposer_account_id=1,
                    confirmer_account_id=2,
                    effect_generation=1,
                    dept="运营一部",
                ),
            )
        )


def test_subject_errors_are_manual_and_unavailable_is_retryable() -> None:
    from app.services.uncertain_resolution import (
        _is_retryable_effect_error,
        _manual_effect_error,
    )

    assert _is_retryable_effect_error(UncertainResolutionConflict("源应用不可用")) is False
    assert _is_retryable_effect_error(ValueError("invalid usage reservation")) is False
    assert _is_retryable_effect_error(ConnectionError("redis down")) is True
    assert _manual_effect_error(UncertainResolutionConflict("处置 generation 已变化")) == (
        "generation_mismatch"
    )


def test_http_send_model_rejects_usage_subject() -> None:
    from app.api.messages import SendRequestModel
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SendRequestModel.model_validate(
            {
                "category": "notice",
                "mobiles": ["13800138000"],
                "content": "通知",
                "biz_id": "http-forge-1",
                "usage_subject": {"kind": "system_effect", "app_id": 88},
            }
        )
