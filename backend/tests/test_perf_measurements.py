"""用受控依赖验证实际发生器和端到端计时，不产生HTTP负载。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import BoundedSemaphore, Event
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import perf_smoke
from perf_smoke import (
    HttpResponse,
    LoadEvent,
    LoadGenerationFailure,
    LoadMeasurement,
    MeasuredResults,
    PerformanceConfig,
    PerformanceFailure,
    PerformanceSuite,
    _runtime_process_document,
    failure_payload,
    run_open_loop,
)

from tests.test_perf_smoke import FakeApi, FakeMock, FakeProbe, StepClock, immediate_scheduler


def test_generator_saturation_stops_submission_and_joins_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, saturated = Event(), Event(), Event()
    now = [0.0]

    class ObservedSemaphore(BoundedSemaphore):
        def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
            result = super().acquire(blocking=blocking, timeout=timeout)
            if not result:
                saturated.set()
            return result

    def handler(_event: LoadEvent) -> str:
        entered.set()
        assert release.wait(2)
        now[0] = 2.0
        return "finished"

    def sleep(delay: float) -> None:
        assert entered.wait(2)
        now[0] += delay

    monkeypatch.setattr(perf_smoke, "BoundedSemaphore", ObservedSemaphore)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_open_loop,
            [LoadEvent(0, 0, "notice"), LoadEvent(1, 0.01, "notice")],
            handler,
            clock=lambda: now[0],
            sleeper=sleep,
            max_workers=1,
        )
        assert saturated.wait(2)
        assert not future.done()
        release.set()
        with pytest.raises(LoadGenerationFailure) as caught:
            future.result()
    result = caught.value.measurement
    assert result.planned_requests == 2 and result.started_requests == 1
    assert result.completed_requests == 1 and result.not_started_requests == 1
    assert result.peak_in_flight == 1 and not result.valid
    assert result.target_rps == 100
    assert result.actual_completed_rps == 0.5


def test_late_handler_and_http_error_never_hide_in_success() -> None:
    now = [0.0]

    def sleeper(delay: float) -> None:
        now[0] += delay + 0.5

    with pytest.raises(LoadGenerationFailure) as late:
        run_open_loop(
            [LoadEvent(0, 1, "notice")], lambda _: 1, clock=lambda: now[0], sleeper=sleeper
        )
    assert late.value.measurement.max_start_lag_s == 0.5

    def failing(_: LoadEvent) -> None:
        raise PerformanceFailure("synthetic HTTP failure")

    with pytest.raises(LoadGenerationFailure) as failed:
        run_open_loop([LoadEvent(0, 0, "notice")], failing)
    assert failed.value.measurement.failed_requests == 1
    assert failed.value.measurement.completed_requests == 1


def test_late_start_stops_remaining_offered_load() -> None:
    handled = Event()
    now = [0.0]
    starts: list[int] = []
    sleeps = [0]

    def sleeper(delay: float) -> None:
        if sleeps[0]:
            assert handled.wait(2)
        sleeps[0] += 1
        now[0] += delay + 0.5

    def handler(event: LoadEvent) -> int:
        starts.append(event.index)
        handled.set()
        return 1

    with pytest.raises(LoadGenerationFailure) as caught:
        run_open_loop(
            [LoadEvent(0, 1, "notice"), LoadEvent(1, 3, "notice"), LoadEvent(2, 5, "notice")],
            handler,
            clock=lambda: now[0],
            sleeper=sleeper,
            max_workers=1,
        )
    assert starts == [0]
    assert caught.value.measurement.not_started_requests == 2
    assert caught.value.measurement.completed_requests == 1
    assert caught.value.measurement.max_start_lag_s == 0.5


def test_verify_gate_includes_api_acceptance_before_mock_observation() -> None:
    now = [0.0]

    class SlowApi(FakeApi):
        def request(self, method: str, path: str, **kwargs: Any) -> HttpResponse:
            if path == "/api/v1/messages/send":
                now[0] += 1.5
            return super().request(method, path, **kwargs)

    class SlowMock(FakeMock):
        def request(self, method: str, path: str, **kwargs: Any) -> HttpResponse:
            if method == "GET":
                now[0] += 1.0
            return super().request(method, path, **kwargs)

    api = SlowApi()
    suite = PerformanceSuite(
        api,
        SlowMock(api),
        FakeProbe(),
        {"app-iam": "a", "app-oa": "b", "app-mkt": "c"},
        config=PerformanceConfig(1, 1, 1, 1),
        scheduler=immediate_scheduler,
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    with pytest.raises(PerformanceFailure, match="verify P95 2.500s"):
        suite._phase_two()
    assert suite._verify_acceptance == [1.5]
    assert suite._verify_post_accept == [1.0]


def test_process_samples_cannot_be_silently_merged_or_unscoped() -> None:
    first = 'sms_runtime_event_loop_delay_seconds{process_instance="a"} 0.001'
    second = 'sms_runtime_process_resident_memory_bytes{process_instance="b"} 100'
    with pytest.raises(PerformanceFailure, match="exactly one process"):
        _runtime_process_document(first + "\n" + second)
    with pytest.raises(PerformanceFailure, match="lacks process identity"):
        _runtime_process_document("sms_runtime_event_loop_delay_seconds 0.001")
    normalized, identity = _runtime_process_document(first)
    assert identity == "a" and normalized == "sms_runtime_event_loop_delay_seconds 0.001"


def test_cancelled_handler_and_interrupted_schedule_are_invalid_and_counted() -> None:
    import asyncio

    def cancelled(_: LoadEvent) -> None:
        raise asyncio.CancelledError

    with pytest.raises(LoadGenerationFailure) as caught:
        run_open_loop([LoadEvent(0, 0, "notice")], cancelled)
    assert caught.value.measurement.cancelled_requests == 1
    assert caught.value.measurement.failed_requests == 1
    assert caught.value.measurement.completed_requests == 1

    def interrupted(_delay: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(LoadGenerationFailure) as interrupted_result:
        run_open_loop(
            [LoadEvent(0, 1, "notice")], lambda _: 1, clock=lambda: 0.0, sleeper=interrupted
        )
    assert interrupted_result.value.measurement.interrupted
    assert interrupted_result.value.measurement.not_started_requests == 1


def _measurement(count: int) -> LoadMeasurement:
    # 测量传播的固定输入；发生器真实计数和窗口由前述测试独立验证。
    return LoadMeasurement(
        planned_requests=count,
        started_requests=count,
        completed_requests=count,
        failed_requests=0,
        cancelled_requests=0,
        interrupted=False,
        not_started_requests=0,
        target_rps=float(count),
        planned_window_s=1.0,
        actual_start_window_s=1.25,
        completion_window_s=1.5,
        actual_started_rps=count / 1.25,
        actual_completed_rps=count / 1.5,
        max_start_lag_s=0.1,
        start_lag_limit_s=0.25,
        peak_in_flight=1,
        valid=True,
    )


@pytest.mark.parametrize("failed_phase", ["acceptance", "verify", "mock", "drain", "cleanup"])
def test_later_gate_failure_preserves_all_acquired_load_measurements(failed_phase: str) -> None:
    clock = StepClock(step=0.01)
    observed: list[LoadMeasurement] = []

    class SelectedFailureApi(FakeApi):
        def request(self, method: str, path: str, **kwargs: Any) -> HttpResponse:
            response = super().request(method, path, **kwargs)
            payload = kwargs.get("payload")
            if path == "/api/v1/messages/send" and isinstance(payload, dict):
                phase = "acceptance" if "scheduled_at" in payload else "verify"
                if phase == failed_phase:
                    clock.value += 3
            return response

    class SelectedFailureMock(FakeMock):
        def request(self, method: str, path: str, **kwargs: Any) -> HttpResponse:
            if failed_phase == "mock" and method == "POST":
                return HttpResponse(500, None)
            return super().request(method, path, **kwargs)

    class SelectedFailureSuite(PerformanceSuite):
        def _phase_three(self) -> float:
            if failed_phase == "drain":
                raise PerformanceFailure("PERF-03 queues did not drain within 1s")
            return super()._phase_three()

    def scheduler(events: Sequence[LoadEvent], handler: Callable[[LoadEvent], Any]) -> list[Any]:
        values = immediate_scheduler(events, handler)
        measurement = _measurement(len(events))
        observed.append(measurement)
        return MeasuredResults(values, measurement)

    api = SelectedFailureApi(cancel_status=500 if failed_phase == "cleanup" else 200)
    suite = SelectedFailureSuite(
        api,
        SelectedFailureMock(api),
        FakeProbe(),
        {"app-iam": "private-api-key", "app-oa": "b", "app-mkt": "c"},
        config=PerformanceConfig(1, 1, 1, 1),
        scheduler=scheduler,
        clock=clock,
    )
    with pytest.raises(PerformanceFailure) as caught:
        suite.run()
    payload = failure_payload(caught.value)
    expected = {"acceptance": asdict(observed[0])}
    if failed_phase not in {"acceptance", "mock"}:
        expected["mixed"] = asdict(observed[1])
    assert payload["load_measurements"] == expected
    assert payload["cleanup_failed"] is (failed_phase == "cleanup")
    assert "private-api-key" not in str(payload)
    assert "188" not in str(payload)


def test_generator_failure_and_cleanup_failure_preserve_counts_without_dependency_text() -> None:
    measurement = replace(_measurement(3), failed_requests=1, valid=False)

    def failing_scheduler(
        _events: Sequence[LoadEvent], _handler: Callable[[LoadEvent], Any]
    ) -> list[Any]:
        raise LoadGenerationFailure(measurement)

    class FailedCleanupSuite(PerformanceSuite):
        def _cleanup_scheduled_batches(self) -> tuple[int, float]:
            raise OSError("synthetic-sensitive-dependency-value")

    api = FakeApi()
    suite = FailedCleanupSuite(
        api,
        FakeMock(api),
        FakeProbe(),
        {"app-iam": "a", "app-oa": "b", "app-mkt": "c"},
        config=PerformanceConfig(1, 1, 1, 1),
        scheduler=failing_scheduler,
    )
    with pytest.raises(PerformanceFailure) as caught:
        suite.run()
    payload = failure_payload(caught.value)
    assert payload["load_measurements"] == {"acceptance": asdict(measurement)}
    assert payload["cleanup_failed"] is True
    assert "OSError" in str(payload["error"])
    assert "synthetic-sensitive-dependency-value" not in str(payload)
