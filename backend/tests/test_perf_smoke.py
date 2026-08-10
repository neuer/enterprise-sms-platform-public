from __future__ import annotations

import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from perf_smoke import (  # noqa: E402
    DrainProbe,
    DrainSnapshot,
    HttpResponse,
    JsonHttpClient,
    LoadEvent,
    PerformanceConfig,
    PerformanceFailure,
    PerformanceSuite,
    _prometheus_value,
    build_acceptance_events,
    build_mixed_events,
    percentile95,
)


class CountingHttpServer(ThreadingHTTPServer):
    connection_count = 0

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        self.connection_count += 1
        return request, client_address


class JsonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def test_json_http_client_reuses_a_bounded_keepalive_connection() -> None:
    server = CountingHttpServer(("127.0.0.1", 0), JsonHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = JsonHttpClient(f"http://127.0.0.1:{server.server_port}")
    try:
        responses = [client.request("GET", "/readyz") for _ in range(3)]
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert responses == [HttpResponse(200, {"status": "ok"})] * 3
    assert server.connection_count == 1


def test_json_http_client_preserves_safe_http_error_parsing() -> None:
    responses = iter(
        (
            httpx.Response(422, content=b'{"code":"INVALID_PARAM"}'),
            httpx.Response(500, content=b"not-json"),
        )
    )
    client = JsonHttpClient(
        "http://performance.test",
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )
    try:
        assert client.request("POST", "/send", payload={}) == HttpResponse(
            422, {"code": "INVALID_PARAM"}
        )
        assert client.request("GET", "/readyz") == HttpResponse(500, None)
    finally:
        client.close()


class FakeRunner:
    def __init__(self, *, active: bytes = b"0\n", queues: bytes = b"0\n0\n0\n") -> None:
        self.active = active
        self.queues = queues
        self.calls: list[list[str]] = []

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        return self.active if "psql" in argv else self.queues


def test_nearest_rank_p95_requires_samples_and_uses_95th_value() -> None:
    assert percentile95([value / 1000 for value in range(1, 101)]) == 0.095
    assert percentile95([0.2]) == 0.2
    with pytest.raises(PerformanceFailure, match="samples"):
        percentile95([])


def test_acceptance_events_are_open_loop_30rps_for_60s_with_235_mix() -> None:
    events = build_acceptance_events(30, 60)
    assert len(events) == 1800
    assert events[0].offset_s == 0
    assert events[-1].offset_s == pytest.approx(1799 / 30)
    assert Counter(event.kind for event in events) == {
        "verify": 360,
        "notice": 540,
        "market": 900,
    }


def test_mixed_events_offer_verify_1rps_and_bulk_3rps_for_60s() -> None:
    events = build_mixed_events(60)
    assert len(events) == 240
    assert Counter(event.kind for event in events) == {"verify": 60, "bulk": 180}
    assert [event.offset_s for event in events[:4]] == [0, 0.25, 0.5, 0.75]
    assert events[-1].offset_s == 59.75


def test_drain_probe_uses_global_active_batches_and_three_celery_queues(tmp_path: Path) -> None:
    runner = FakeRunner()
    snapshot = DrainProbe(
        runner,
        compose_file=tmp_path / "docker-compose.yml",
        repository_root=tmp_path,
    ).snapshot()

    assert snapshot.active_batches == 0
    assert snapshot.queues == {"realtime": 0, "bulk": 0, "callback": 0}
    sql_call, redis_call = runner.calls
    assert "status IN ('queued','sending')" in " ".join(sql_call)
    assert "phone" not in " ".join(sql_call).casefold()
    assert "LLEN" in " ".join(redis_call)
    assert redis_call[-8:-3] == ["exec", "-T", "redis", "sh", "-ec"]
    assert "--user sms_broker --askpass" in redis_call[-3]
    assert "/run/secrets/redis_broker_password" in redis_call[-3]


@pytest.mark.parametrize(
    ("active", "queues"),
    [(b"not-a-count", b"0\n0\n0\n"), (b"0\n", b"0\n1\n")],
)
def test_drain_probe_fails_closed_on_invalid_safe_counts(
    tmp_path: Path, active: bytes, queues: bytes
) -> None:
    with pytest.raises(PerformanceFailure, match="PERF-03"):
        DrainProbe(
            FakeRunner(active=active, queues=queues),
            compose_file=tmp_path / "compose.yml",
            repository_root=tmp_path,
        ).snapshot()


class StepClock:
    def __init__(self, step: float = 0.01) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class FakeApi:
    def __init__(self, *, cancel_status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, Mapping[str, str] | None]] = []
        self.sent_prefixes: list[str] = []
        self.scheduled_keys: dict[str, str] = {}
        self.cancelled: list[tuple[str, object]] = []
        self.cancel_status = cancel_status
        self.sequence = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        typed_payload = payload if isinstance(payload, dict) else None
        self.calls.append((method, path, typed_payload, headers))
        if path == "/api/v1/web/auth/login":
            assert typed_payload is not None and typed_payload["provider_code"] == "ad"
            return HttpResponse(200, {"token": "operator-token"})
        if path == "/metrics":
            return HttpResponse(
                200,
                "\n".join(
                    (
                        "sms_runtime_event_loop_delay_seconds 0.012",
                        "sms_runtime_process_resident_memory_bytes 104857600",
                        'sms_runtime_database_connections{state="open"} 4',
                        'sms_runtime_database_connections{state="checked_out"} 1',
                        'sms_runtime_redis_connections{state="open"} 3',
                        'sms_runtime_redis_connections{state="in_use"} 1',
                    )
                ),
            )
        if path == "/api/v1/web/messages/send":
            return HttpResponse(200, {"status": "queued", "batch_no": "b" * 32})
        if path == "/api/v1/messages/send":
            assert typed_payload is not None and headers is not None
            self.sequence += 1
            batch_no = f"{self.sequence:032x}"
            if "scheduled_at" in typed_payload:
                self.scheduled_keys[batch_no] = headers["X-Api-Key"]
                return HttpResponse(200, {"status": "scheduled", "batch_no": batch_no})
            self.sent_prefixes.append(batch_no[:24])
            return HttpResponse(200, {"status": "queued", "batch_no": batch_no})
        if (
            method == "POST"
            and path.startswith("/api/v1/messages/batches/")
            and path.endswith("/cancel")
        ):
            self.cancelled.append((path, headers))
            return HttpResponse(self.cancel_status, None)
        raise AssertionError(path)


class ConcurrencyApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.active_cancellations = 0
        self.peak_cancellations = 0
        self.concurrency_lock = Lock()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if method == "POST" and path.endswith("/cancel"):
            with self.concurrency_lock:
                self.active_cancellations += 1
                self.peak_cancellations = max(
                    self.peak_cancellations, self.active_cancellations
                )
            try:
                time.sleep(0.02)
                return super().request(method, path, payload=payload, headers=headers)
            finally:
                with self.concurrency_lock:
                    self.active_cancellations -= 1
        return super().request(method, path, payload=payload, headers=headers)


class MixedCleanupFailureApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_sequence = 0
        self.cancel_lock = Lock()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if method == "POST" and path.endswith("/cancel"):
            with self.cancel_lock:
                self.cancel_sequence += 1
                sequence = self.cancel_sequence
                self.cancelled.append((path, headers))
            if sequence == 1:
                return HttpResponse(500, None)
            if sequence == 2:
                raise PerformanceFailure("injected transport failure")
            return HttpResponse(200, None)
        return super().request(method, path, payload=payload, headers=headers)


class FakeMock:
    def __init__(self, api: FakeApi) -> None:
        self.api = api

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        assert path == "/_mock/state"
        if method == "POST":
            self.api.sent_prefixes.clear()
        return HttpResponse(
            200,
            {
                "send_calls": [
                    {"customId": f"{prefix}00000001"} for prefix in self.api.sent_prefixes
                ]
            },
        )


class ResetFailingMock(FakeMock):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if method == "POST":
            return HttpResponse(500, None)
        return super().request(method, path, payload=payload, headers=headers)


class FakeProbe:
    def worker_config(self) -> tuple[int, int]:
        return (5, 2)

    def snapshot(self) -> DrainSnapshot:
        return DrainSnapshot(0, {"realtime": 0, "bulk": 0, "callback": 0})


class InitiallyBusyProbe(FakeProbe):
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> DrainSnapshot:
        self.calls += 1
        active = 1 if self.calls == 1 else 0
        return DrainSnapshot(
            active,
            {"realtime": active, "bulk": 0, "callback": 0},
        )


def immediate_scheduler(
    events: Sequence[LoadEvent], handler: Callable[[LoadEvent], Any]
) -> list[Any]:
    return [handler(event) for event in events]


def test_performance_suite_runs_three_isolated_phases_with_safe_results() -> None:
    api = FakeApi()
    clock = StepClock()
    result = PerformanceSuite(
        api,
        FakeMock(api),
        FakeProbe(),
        {"app-iam": "iam-key", "app-oa": "oa-key", "app-mkt": "mkt-key"},
        config=PerformanceConfig(10, 1, 1, 2),
        scheduler=immediate_scheduler,
        clock=clock,
        sleeper=lambda _seconds: None,
        run_id="fixed",
    ).run()

    assert result.acceptance_requests == 10
    assert result.verify_requests == 1 and result.bulk_requests == 3
    assert result.cancelled_scheduled_batches == 10
    scheduled = [
        payload
        for _method, path, payload, _headers in api.calls
        if path == "/api/v1/messages/send" and payload is not None and "scheduled_at" in payload
    ]
    assert Counter(item["category"] for item in scheduled) == {
        "verify": 2,
        "notice": 3,
        "market": 5,
    }
    web_sends = [
        payload
        for _method, path, payload, _headers in api.calls
        if path == "/api/v1/web/messages/send" and payload is not None
    ]
    assert [item["biz_id"] for item in web_sends] == [
        "pfw-fixed-1",
        "pfw-fixed-2",
        "pfw-fixed-3",
    ]
    assert len(api.cancelled) == 10
    for path, headers in api.cancelled:
        batch_no = path.removeprefix("/api/v1/messages/batches/").removesuffix("/cancel")
        assert headers == {"X-Api-Key": api.scheduled_keys[batch_no]}
    assert result.acceptance_p95_s < 0.3
    assert result.verify_p95_s < 2
    assert result.event_loop_delay_seconds == 0.012
    assert result.process_resident_memory_bytes == 104857600
    assert result.database_connections_open == 4
    assert result.database_connections_checked_out == 1
    assert result.redis_connections_open == 3
    assert result.redis_connections_in_use == 1
    assert result.cleanup_seconds >= 0


def test_performance_suite_waits_for_previous_gate_workload_to_drain() -> None:
    api = FakeApi()
    probe = InitiallyBusyProbe()

    PerformanceSuite(
        api,
        FakeMock(api),
        probe,
        {"app-iam": "iam-key", "app-oa": "oa-key", "app-mkt": "mkt-key"},
        config=PerformanceConfig(1, 1, 1, 2),
        scheduler=immediate_scheduler,
        clock=StepClock(),
        sleeper=lambda _seconds: None,
        run_id="pre-drain",
    ).run()

    assert probe.calls >= 3


def test_prometheus_sample_parser_fails_closed() -> None:
    assert _prometheus_value("metric_name 1.25\n", "metric_name") == 1.25
    with pytest.raises(PerformanceFailure, match="incomplete"):
        _prometheus_value("other_metric 1\n", "metric_name")
    with pytest.raises(PerformanceFailure, match="invalid"):
        _prometheus_value("metric_name NaN\n", "metric_name")


def test_cleanup_defaults_to_sixty_four_workers_and_validates_bounds() -> None:
    assert PerformanceConfig().cleanup_workers == 64

    for workers in (0, 65):
        with pytest.raises(ValueError, match="cleanup_workers"):
            PerformanceConfig(cleanup_workers=workers).validate()


def test_cleanup_is_bounded_concurrent_and_deduplicates_batches() -> None:
    api = ConcurrencyApi()
    suite = PerformanceSuite(
        api,
        FakeMock(api),
        FakeProbe(),
        {"app-iam": "iam-key", "app-oa": "oa-key", "app-mkt": "mkt-key"},
        config=PerformanceConfig(cleanup_workers=3),
        scheduler=immediate_scheduler,
    )
    scheduled = [(f"batch-{index}", "app-iam") for index in range(6)]
    suite._scheduled_batches = [*scheduled, scheduled[0]]

    cancelled, cleanup_seconds = suite._cleanup_scheduled_batches()

    assert cancelled == 6
    assert cleanup_seconds >= 0
    assert len(api.cancelled) == 6
    assert len({path for path, _headers in api.cancelled}) == 6
    assert 1 < api.peak_cancellations <= 3
    assert suite._scheduled_batches == []


def test_cleanup_aggregates_http_and_transport_failures_without_identifiers() -> None:
    api = MixedCleanupFailureApi()
    suite = PerformanceSuite(
        api,
        FakeMock(api),
        FakeProbe(),
        {"app-iam": "iam-secret", "app-oa": "oa-secret", "app-mkt": "mkt-secret"},
        config=PerformanceConfig(cleanup_workers=3),
        scheduler=immediate_scheduler,
    )
    identifiers = ("secret-batch-one", "secret-batch-two", "secret-batch-three")
    suite._scheduled_batches = [(identifier, "app-iam") for identifier in identifiers]

    with pytest.raises(PerformanceFailure, match="PERF-04") as captured:
        suite._cleanup_scheduled_batches()

    summary = str(captured.value)
    assert "failed=2" in summary
    assert "total=3" in summary
    assert "cleanup_seconds=" in summary
    assert "outcomes=http_500:1,transport:1" in summary
    for sensitive in (*identifiers, "iam-secret", "oa-secret", "mkt-secret"):
        assert sensitive not in summary
    assert len(api.cancelled) == 3
    assert len(suite._scheduled_batches) == 2


def test_scheduled_cleanup_waits_for_queue_drain_and_both_must_finish() -> None:
    drain_finished = Event()

    class OverlapSuite(PerformanceSuite):
        def _phase_one(self) -> tuple[int, float]:
            return 1, 0.01

        def _phase_two(self) -> tuple[int, int, float]:
            return 1, 3, 0.01

        def _phase_three(self) -> float:
            drain_finished.set()
            return 0.2

        def _runtime_metrics(self) -> tuple[float, int, int, int, int, int]:
            return (0.01, 100, 2, 1, 2, 1)

        def _cleanup_scheduled_batches(self) -> tuple[int, float]:
            assert drain_finished.is_set()
            return 1, 0.3

    api = FakeApi()
    result = OverlapSuite(
        api,
        FakeMock(api),
        FakeProbe(),
        {"app-iam": "iam-key", "app-oa": "oa-key", "app-mkt": "mkt-key"},
        scheduler=immediate_scheduler,
    ).run()

    assert result.drain_seconds == 0.2
    assert result.event_loop_delay_seconds == 0.01
    assert result.cleanup_seconds == 0.3
    assert result.cancelled_scheduled_batches == 1


def test_performance_suite_cleans_scheduled_batches_after_phase_failure() -> None:
    api = FakeApi()
    with pytest.raises(PerformanceFailure, match="mock reset failed"):
        PerformanceSuite(
            api,
            ResetFailingMock(api),
            FakeProbe(),
            {"app-iam": "iam-key", "app-oa": "oa-key", "app-mkt": "mkt-key"},
            config=PerformanceConfig(10, 1, 1, 2),
            scheduler=immediate_scheduler,
            clock=StepClock(),
            sleeper=lambda _seconds: None,
            run_id="phase-failure",
        ).run()

    assert len(api.cancelled) == 10


def test_performance_suite_fails_closed_without_exposing_cleanup_material() -> None:
    api = FakeApi(cancel_status=500)
    with pytest.raises(PerformanceFailure, match="cleanup failed") as captured:
        PerformanceSuite(
            api,
            FakeMock(api),
            FakeProbe(),
            {"app-iam": "iam-key", "app-oa": "oa-key", "app-mkt": "mkt-key"},
            config=PerformanceConfig(1, 1, 1, 2),
            scheduler=immediate_scheduler,
            clock=StepClock(),
            sleeper=lambda _seconds: None,
            run_id="cleanup-failure",
        ).run()

    summary = str(captured.value)
    assert "00000000000000000000000000000001" not in summary
    assert "iam-key" not in summary
    assert "oa-key" not in summary
    assert "mkt-key" not in summary


def test_performance_suite_rejects_wrong_vendor_qps() -> None:
    class WrongProbe(FakeProbe):
        def worker_config(self) -> tuple[int, int]:
            return (6, 2)

    api = FakeApi()
    with pytest.raises(PerformanceFailure, match="PERF-00"):
        PerformanceSuite(
            api,
            FakeMock(api),
            WrongProbe(),
            {"app-iam": "iam", "app-oa": "oa", "app-mkt": "mkt"},
            config=PerformanceConfig(1, 1, 1, 1),
            scheduler=immediate_scheduler,
        ).run()
