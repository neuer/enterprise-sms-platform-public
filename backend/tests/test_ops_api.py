from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import app.api.ops as ops_api
from app.api.reports import get_export_service
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.roles import Role
from app.core.auth.runtime import get_auth_facade
from app.core.jobtrack import JobSpec
from app.main import create_app
from app.services.export import ExportTaskInfo
from app.services.ops import (
    AlertRecord,
    JobOpsService,
    JobRecord,
    JobRoute,
    OpsPage,
    QueueResumeResult,
    QueueSnapshot,
    RawLogRecord,
    UncertainRecord,
    UnmatchedRecord,
)
from app.services.ops_repository import SqlOpsRepository
from app.services.outbox import OutboxEventPage, OutboxEventRecord, OutboxStats

NOW = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
PUBLIC_ID = UUID("c0a80101-0000-4000-8000-000000000134")


class FakeFacade:
    def __init__(self, role: Role = "admin") -> None:
        self.role: Role = role

    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims(
            1,
            101,
            "local",
            "admin01",
            "管理员",
            "平台部",
            self.role,
            1,
            "admin-session",
        )


class FakeRepository:
    async def list_alerts(self, query: Any) -> OpsPage[AlertRecord]:
        return OpsPage(
            (
                AlertRecord(
                    1,
                    "job_failed",
                    "crit",
                    "任务失败",
                    {"job_name": "poll_report"},
                    "log-sink",
                    NOW,
                ),
            ),
            1,
            query.page,
            query.page_size,
        )

    async def list_raw_logs(self, query: Any) -> OpsPage[RawLogRecord]:
        return OpsPage(
            (RawLogRecord(2, "report", 1, 1, False, "ValueError", NOW),),
            1,
            query.page,
            query.page_size,
        )

    async def list_uncertain(
        self,
        page: int,
        page_size: int,
    ) -> OpsPage[UncertainRecord]:
        return OpsPage(
            (UncertainRecord(3, "BATCH-1", "CUSTOM-1", 50, None, NOW, 3600),), 1, page, page_size
        )


class FakeOpsService:
    async def list_unmatched(
        self,
        phone: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        page_size: int,
    ) -> OpsPage[UnmatchedRecord]:
        return OpsPage(
            (UnmatchedRecord(4, "vendor-1", "legacy-1", "138****8000", 1, "DELIVRD", NOW, NOW),),
            1,
            page,
            page_size,
        )


class FakeJobs:
    def __init__(self) -> None:
        self.triggered: list[tuple[str, str, str]] = []

    async def list(self) -> tuple[JobRecord, ...]:
        return (JobRecord("poll_report", NOW, "success", 120, 1, 1.0, False),)

    async def trigger(
        self,
        job_name: str,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> None:
        self.triggered.append((job_name, actor, ip, principal.account_id))


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, str, str]] = []

    async def status(self) -> QueueSnapshot:
        return QueueSnapshot("999", "999", 20000, 10000)

    async def resume(
        self,
        *,
        force: bool,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> QueueResumeResult:
        self.calls.append((force, actor, ip, principal.account_id, principal.identity_id))
        return QueueResumeResult(2, ("999",))


class FakeReplay:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.reevaluations: list[tuple[int, str, str]] = []

    async def replay(
        self,
        raw_id: int,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> int:
        self.calls.append((raw_id, actor, ip, principal.account_id, principal.identity_id))
        return 3

    async def reevaluate(
        self,
        raw_id: int,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> object:
        self.reevaluations.append(
            (raw_id, actor, ip, principal.account_id, principal.identity_id)
        )
        return type(
            "Disposition",
            (),
            {
                "parse_state": "unattempted",
                "replay_eligibility": "automatic",
                "reason": "unattempted",
            },
        )()


class FakeExport:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, bool, SecurityPrincipal]] = []

    async def create(
        self,
        filters: Any,
        *,
        decrypted: bool,
        principal: SecurityPrincipal,
    ) -> ExportTaskInfo:
        self.calls.append((filters, decrypted, principal))
        return ExportTaskInfo(
            9,
            PUBLIC_ID,
            "pending",
            decrypted,
            None,
            None,
            None,
            NOW,
        )


class FakeOutbox:
    def __init__(self) -> None:
        self.retried: list[tuple[UUID, SecurityPrincipal]] = []
        self.retry_result = True
        self.list_calls: list[tuple[str | None, int, int]] = []

    async def stats(self) -> OutboxStats:
        return OutboxStats(3, 2, 1, 4, 7, 301)

    async def list_events(
        self,
        state: str | None,
        page: int,
        page_size: int,
    ) -> OutboxEventPage:
        self.list_calls.append((state, page, page_size))
        return OutboxEventPage(
            (
                OutboxEventRecord(
                    PUBLIC_ID,
                    "usage.release",
                    "usage_reservation",
                    "c0a80101-0000-4000-8000-000000000134",
                    "app.tasks.outbox.release_usage",
                    "realtime",
                    "dead",
                    12,
                    12,
                    3,
                    "BrokerTimeout",
                    NOW,
                    NOW,
                    NOW,
                ),
            ),
            1,
            page,
            page_size,
        )

    async def retry_dead(
        self,
        event_id: UUID,
        *,
        principal: SecurityPrincipal,
    ) -> bool:
        self.retried.append((event_id, principal))
        return self.retry_result


def client(role: Role = "admin") -> tuple[TestClient, dict[str, Any]]:
    app = create_app()
    values = {
        "repo": FakeRepository(),
        "ops": FakeOpsService(),
        "jobs": FakeJobs(),
        "queue": FakeQueue(),
        "replay": FakeReplay(),
        "export": FakeExport(),
        "outbox": FakeOutbox(),
    }
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[ops_api.get_ops_repository] = lambda: values["repo"]
    app.dependency_overrides[ops_api.get_ops_service] = lambda: values["ops"]
    app.dependency_overrides[ops_api.get_job_ops_service] = lambda: values["jobs"]
    app.dependency_overrides[ops_api.get_queue_recovery_service] = lambda: values["queue"]
    app.dependency_overrides[ops_api.get_raw_replay_service] = lambda: values["replay"]
    app.dependency_overrides[get_export_service] = lambda: values["export"]
    app.dependency_overrides[ops_api.get_outbox_repository] = lambda: values["outbox"]
    return TestClient(app), values


def test_ops_lists_return_safe_complete_models() -> None:
    browser, _ = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    assert (
        browser.get("/api/v1/web/admin/alerts", headers=headers).json()["items"][0]["level"]
        == "crit"
    )
    raw = browser.get("/api/v1/web/admin/raw-logs", headers=headers).json()
    assert raw["items"][0]["custom_id_count"] == 1
    assert "payload" not in str(raw).lower()
    assert (
        browser.get("/api/v1/web/admin/chunks/uncertain", headers=headers).json()["items"][0][
            "age_seconds"
        ]
        == 3600
    )
    unmatched = browser.post(
        "/api/v1/web/admin/unmatched-reports", headers=headers, json={"page": 1, "page_size": 20}
    ).json()
    assert unmatched["items"][0]["phone_mask"] == "138****8000"
    assert (
        browser.get("/api/v1/web/admin/jobs", headers=headers).json()[0]["job_name"]
        == "poll_report"
    )
    queue = browser.get("/api/v1/web/admin/queue/status", headers=headers).json()
    assert queue == {
        "realtime_code": "999",
        "bulk_code": "999",
        "balance": 20000,
        "threshold": 10000,
    }


def test_ops_writes_are_audited_and_return_contract_statuses() -> None:
    browser, values = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    replay = browser.post("/api/v1/web/admin/raw-logs/2/replay", headers=headers)
    assert replay.status_code == 200 and replay.json() == {"processed_items": 3}
    trigger = browser.post("/api/v1/web/admin/jobs/poll_report/trigger", headers=headers)
    repeated = browser.post("/api/v1/web/admin/jobs/poll_report/trigger", headers=headers)
    assert trigger.status_code == 202
    assert repeated.status_code == 202
    assert [item[0] for item in values["jobs"].triggered] == ["poll_report", "poll_report"]
    assert all(item[3] == 1 for item in values["jobs"].triggered)
    resume = browser.post("/api/v1/web/admin/queue/resume?force=true", headers=headers)
    assert resume.json() == {"resumed_batches": 2, "paused_codes": ["999"]}
    exported = browser.post(
        "/api/v1/web/admin/unmatched-reports/export",
        headers=headers,
        json={"phone": "13800138000", "decrypted": True},
    )
    assert exported.status_code == 202
    assert exported.json()["id"] == str(PUBLIC_ID)
    assert values["export"].calls[0][2].account_id == 1

    reevaluate = browser.post("/api/v1/web/admin/raw-logs/2/reevaluate", headers=headers)
    assert reevaluate.status_code == 200
    assert reevaluate.json() == {
        "parse_state": "unattempted",
        "replay_eligibility": "automatic",
        "reason": "unattempted",
        "parser_version": 1,
    }
    assert vars(ops_api.replay_raw)["__audited_action__"] == "raw_replay"
    assert vars(ops_api.reevaluate_raw)["__audited_action__"] == "raw_reevaluate"
    assert vars(ops_api.trigger_job)["__audited_action__"] == "job_trigger"
    assert vars(ops_api.resume_queue)["__audited_action__"] == "queue_resume"
    assert vars(ops_api.create_unmatched_export)["__audited_action__"] == "export_create"
    assert values["queue"].calls[0][0] is True
    assert values["queue"].calls[0][1] == "admin01"
    assert values["queue"].calls[0][3:] == (1, 101)
    assert values["replay"].calls[0][0] == 2
    assert values["replay"].calls[0][1] == "admin01"
    assert values["replay"].calls[0][3:] == (1, 101)


def test_repeated_poll_report_trigger_binds_live_principal_and_returns_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UAT-20 在同一进程再次 trigger poll_report 时，必须 202 而不是审计 check 500。"""

    bound: list[dict[str, object]] = []
    events: list[Any] = []
    sent: list[tuple[str, str]] = []

    class RecordingSender:
        async def send(self, task_name: str, queue: str) -> None:
            sent.append((task_name, queue))

    class StubEngine:
        def begin(self) -> Any:
            return _Begin()

        async def dispose(self) -> None:
            return None

    class _Begin:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    async def fake_bind(_connection: object, **kwargs: object) -> None:
        bound.append(dict(kwargs))

    async def fake_insert(_connection: object, event: object) -> None:
        events.append(event)

    monkeypatch.setattr(
        "app.services.ops_repository.bind_connection_audit_subject",
        fake_bind,
    )
    monkeypatch.setattr("app.services.ops_repository.insert_audit", fake_insert)

    repository = SqlOpsRepository()
    repository._engine = lambda: StubEngine()  # type: ignore[method-assign]
    service = JobOpsService(
        repository,
        RecordingSender(),
        {"poll_report": JobSpec("poll_report", 60)},
        {"poll_report": JobRoute("app.tasks.poll_report", "realtime")},
        clock=lambda: NOW,
    )
    app = create_app()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[ops_api.get_job_ops_service] = lambda: service
    browser = TestClient(app)
    headers = {"Authorization": "Bearer admin.jwt"}

    first = browser.post("/api/v1/web/admin/jobs/poll_report/trigger", headers=headers)
    second = browser.post("/api/v1/web/admin/jobs/poll_report/trigger", headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.headers.get("content-type") is None or first.text == ""
    assert bound == [
        {
            "subject_kind": "human",
            "actor_name": "admin01",
            "account_id": 1,
            "identity_id": 101,
        }
    ] * 2
    assert [event.action for event in events] == ["job_trigger", "job_trigger"]
    assert [event.object_id for event in events] == ["poll_report", "poll_report"]
    assert sent == [
        ("app.tasks.poll_report", "realtime"),
        ("app.tasks.poll_report", "realtime"),
    ]


def test_outbox_status_and_admin_retry_use_stable_principal() -> None:
    browser, values = client()
    headers = {"Authorization": "Bearer admin.jwt"}
    event_id = UUID("c0a80101-0000-4000-8000-000000000135")

    status_response = browser.get("/api/v1/web/admin/outbox", headers=headers)
    assert status_response.json() == {
        "pending": 3,
        "published": 2,
        "processing": 1,
        "dead": 4,
        "failed_attempts": 7,
        "oldest_age_seconds": 301,
    }
    retry_response = browser.post(
        f"/api/v1/web/admin/outbox/{event_id}/retry",
        headers=headers,
    )
    assert retry_response.status_code == 204
    retried_id, principal = values["outbox"].retried[0]
    assert retried_id == event_id
    assert principal.account_id == 1
    assert principal.identity_id == 101


def test_outbox_retry_rejects_non_dead_state() -> None:
    browser, values = client()
    values["outbox"].retry_result = False

    response = browser.post(
        "/api/v1/web/admin/outbox/c0a80101-0000-4000-8000-000000000135/retry",
        headers={"Authorization": "Bearer admin.jwt"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"


def test_outbox_events_listing_filters_state_and_hides_args() -> None:
    browser, values = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    response = browser.get(
        "/api/v1/web/admin/outbox/events?state=dead&page=2",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1 and body["page"] == 2
    item = body["items"][0]
    assert item["id"] == str(PUBLIC_ID)
    assert item["state"] == "dead"
    assert item["attempts"] == 12 and item["max_attempts"] == 12
    assert item["last_error"] == "BrokerTimeout"
    assert "args" not in str(body).lower()
    assert "dedup_key" not in str(body)
    assert "correlation_id" not in str(body)
    assert values["outbox"].list_calls == [("dead", 2, 20)]


def test_all_ops_routes_are_admin_only() -> None:
    browser, _ = client("viewer")
    response = browser.get(
        "/api/v1/web/admin/raw-logs",
        headers={"Authorization": "Bearer viewer.jwt"},
    )
    assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"
    events = browser.get(
        "/api/v1/web/admin/outbox/events",
        headers={"Authorization": "Bearer viewer.jwt"},
    )
    assert events.status_code == 403 and events.json()["code"] == "FORBIDDEN"
