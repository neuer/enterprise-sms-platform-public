#!/usr/bin/env python3
"""三阶段有界性能冒烟：API 受理、verify 延迟、最终排空。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import subprocess
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from runtime_credentials import read_secret_file

ACCEPTANCE_P95_LIMIT_SECONDS = 2.0
TAB_ID = "00000000000000000000000000000001"


class PerformanceFailure(RuntimeError):
    """性能失败只公开阶段与聚合指标，不回显请求或敏感载荷。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.load_measurements: dict[str, LoadMeasurement] = {}
        self.cleanup_failed = False


class Runner(Protocol):
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes: ...


class CommandRunner:
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty argv sequence")
        argv = [str(item) for item in command]
        result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
        if result.returncode != 0:
            raise PerformanceFailure(
                f"PERF-03 probe command failed: {Path(argv[0]).name} rc={result.returncode}"
            )
        return result.stdout


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    data: object


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
            transport=transport,
            trust_env=False,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        request_headers = {"Accept": "application/json", **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            request_headers["Content-Type"] = "application/json"
        try:
            response = self._client.request(
                method,
                path,
                content=body,
                headers=request_headers,
            )
        except httpx.RequestError as error:
            raise PerformanceFailure("performance HTTP dependency unavailable") from error
        raw = response.content
        content_type = response.headers.get("content-type", "").partition(";")[0].strip()
        data: object
        if response.status_code >= 400:
            try:
                data = json.loads(raw) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None
        elif not raw:
            data = None
        elif content_type.casefold() == "application/json":
            data = json.loads(raw)
        else:
            data = raw.decode("utf-8")
        return HttpResponse(response.status_code, data)

    def close(self) -> None:
        self._client.close()


def percentile95(samples: Sequence[float]) -> float:
    """使用 nearest-rank 计算 P95；空样本不允许伪装为 0。"""

    if not samples:
        raise PerformanceFailure("performance samples are empty")
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def percentile(samples: Sequence[float], percentage: float) -> float:
    """使用 nearest-rank 计算指定分位数，供失败诊断复用。"""

    if not samples:
        raise PerformanceFailure("performance samples are empty")
    if not 0 < percentage <= 1:
        raise ValueError("percentage must be in (0, 1]")
    ordered = sorted(samples)
    return ordered[math.ceil(percentage * len(ordered)) - 1]


def acceptance_failure_detail(samples: Sequence[float], *, rps: int) -> str:
    """输出无请求内容的聚合延迟，区分全程慢与短时宿主抖动。"""

    if rps < 1:
        raise ValueError("acceptance rate must be positive")
    window_size = rps * 10
    window_p95 = [
        percentile95(samples[index : index + window_size])
        for index in range(0, len(samples), window_size)
    ]
    windows = ",".join(f"{value:.3f}" for value in window_p95)
    return (
        f"p50={percentile(samples, 0.50):.3f}s "
        f"p90={percentile(samples, 0.90):.3f}s "
        f"p95={percentile95(samples):.3f}s "
        f"p99={percentile(samples, 0.99):.3f}s "
        f"max={max(samples):.3f}s window_p95=[{windows}]"
    )


@dataclass(frozen=True, slots=True)
class LoadEvent:
    index: int
    offset_s: float
    kind: Literal["verify", "notice", "market", "bulk"]


def build_acceptance_events(rps: int, seconds: int) -> list[LoadEvent]:
    if rps < 1 or seconds < 1:
        raise ValueError("acceptance rate and duration must be positive")
    cycle: tuple[Literal["verify", "notice", "market"], ...] = (
        "verify",
        "verify",
        "notice",
        "notice",
        "notice",
        "market",
        "market",
        "market",
        "market",
        "market",
    )
    return [
        LoadEvent(index, index / rps, cycle[index % len(cycle)]) for index in range(rps * seconds)
    ]


def build_mixed_events(seconds: int) -> list[LoadEvent]:
    if seconds < 1:
        raise ValueError("mixed duration must be positive")
    events: list[LoadEvent] = []
    for second in range(seconds):
        base = len(events)
        events.extend(
            (
                LoadEvent(base, float(second), "verify"),
                LoadEvent(base + 1, second + 0.25, "bulk"),
                LoadEvent(base + 2, second + 0.5, "bulk"),
                LoadEvent(base + 3, second + 0.75, "bulk"),
            )
        )
    return events


@dataclass(frozen=True, slots=True)
class LoadMeasurement:
    planned_requests: int
    started_requests: int
    completed_requests: int
    failed_requests: int
    cancelled_requests: int
    interrupted: bool
    not_started_requests: int
    target_rps: float
    planned_window_s: float
    actual_start_window_s: float
    completion_window_s: float
    actual_started_rps: float
    actual_completed_rps: float
    max_start_lag_s: float
    start_lag_limit_s: float
    peak_in_flight: int
    valid: bool


class MeasuredResults[T](list[T]):
    def __init__(self, values: Sequence[T], measurement: LoadMeasurement) -> None:
        super().__init__(values)
        self.measurement = measurement


class LoadGenerationFailure(PerformanceFailure):
    def __init__(self, measurement: LoadMeasurement) -> None:
        super().__init__("PERF-00 offered load was not achieved or requests failed")
        self.measurement = measurement


def run_open_loop[T](
    events: Sequence[LoadEvent],
    handler: Callable[[LoadEvent], T],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    max_workers: int = 64,
    max_start_lag_s: float = 0.25,
) -> MeasuredResults[T]:
    """固定时点、有界在途；发生器积压或迟到时排完已提交请求并判无效。"""

    if max_workers < 1 or not math.isfinite(max_start_lag_s) or max_start_lag_s < 0:
        raise ValueError("invalid load generator bounds")
    if (
        not events
        or any(not math.isfinite(event.offset_s) or event.offset_s < 0 for event in events)
        or any(
            left.offset_s > right.offset_s for left, right in zip(events, events[1:], strict=False)
        )
    ):
        raise ValueError("load events must be nonempty and ordered")
    started = clock()
    slots = BoundedSemaphore(max_workers)
    guard = Lock()
    late_start = Event()
    starts: list[float] = []
    completions: list[float] = []
    lags: list[float] = []
    failures = 0
    cancellations = 0
    interrupted = False
    in_flight = 0
    peak_in_flight = 0

    def execute(event: LoadEvent) -> T:
        nonlocal failures, cancellations, in_flight
        actual = clock()
        with guard:
            starts.append(actual)
            lag = max(0.0, actual - started - event.offset_s)
            lags.append(lag)
            if lag > max_start_lag_s:
                late_start.set()
        try:
            return handler(event)
        except BaseException as error:
            with guard:
                failures += 1
                cancellations += isinstance(error, (CancelledError, asyncio.CancelledError))
            raise
        finally:
            with guard:
                completions.append(clock())
                in_flight -= 1
            slots.release()

    values: list[T] = []
    futures: list[Future[T]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            for event in events:
                delay = started + event.offset_s - clock()
                if delay > 0:
                    sleeper(delay)
                if late_start.is_set() or not slots.acquire(blocking=False):
                    break
                with guard:
                    in_flight += 1
                    peak_in_flight = max(peak_in_flight, in_flight)
                futures.append(executor.submit(execute, event))
        except KeyboardInterrupt:
            interrupted = True
        for future in futures:
            try:
                values.append(future.result())
            except KeyboardInterrupt:
                interrupted = True
            except (Exception, asyncio.CancelledError):
                # 聚合失败，不输出依赖异常正文；所有已提交写入仍须完成收尾。
                pass
    spacing = (events[-1].offset_s - events[0].offset_s) / max(1, len(events) - 1)
    planned_window = max(events[-1].offset_s + spacing, 1e-9)
    launch_window = max(planned_window, max(starts, default=started) - started + spacing)
    completion_window = max(planned_window, max(completions, default=started) - started)
    measurement = LoadMeasurement(
        planned_requests=len(events),
        started_requests=len(starts),
        completed_requests=len(completions),
        failed_requests=failures,
        cancelled_requests=cancellations,
        interrupted=interrupted,
        not_started_requests=len(events) - len(starts),
        target_rps=len(events) / planned_window,
        planned_window_s=planned_window,
        actual_start_window_s=launch_window,
        completion_window_s=completion_window,
        actual_started_rps=len(starts) / launch_window,
        actual_completed_rps=len(completions) / completion_window,
        max_start_lag_s=max(lags, default=0.0),
        start_lag_limit_s=max_start_lag_s,
        peak_in_flight=peak_in_flight,
        valid=len(starts) == len(events)
        and failures == 0
        and not interrupted
        and max(lags, default=0) <= max_start_lag_s,
    )
    if not measurement.valid:
        raise LoadGenerationFailure(measurement)
    return MeasuredResults(values, measurement)


@dataclass(frozen=True, slots=True)
class DrainSnapshot:
    active_batches: int
    queues: dict[str, int]

    @property
    def empty(self) -> bool:
        return self.active_batches == 0 and all(value == 0 for value in self.queues.values())


class Probe(Protocol):
    def snapshot(self) -> DrainSnapshot: ...

    def worker_config(self) -> tuple[int, int]: ...


class DrainProbe:
    def __init__(
        self,
        runner: Runner,
        *,
        compose_file: Path,
        repository_root: Path,
    ) -> None:
        self.runner = runner
        self.compose_file = compose_file
        self.repository_root = repository_root

    def _compose(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "--profile",
            "dev",
            *arguments,
        ]

    @staticmethod
    def _count(output: bytes) -> int:
        value = output.decode("ascii", errors="strict").strip()
        if not value.isdecimal():
            raise PerformanceFailure("PERF-03 probe returned an invalid count")
        return int(value)

    def snapshot(self) -> DrainSnapshot:
        active = self._count(
            self.runner.run(
                self._compose(
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    "sms_owner",
                    "-d",
                    "sms",
                    "-Atc",
                    "SELECT count(*) FROM sms_batch WHERE status IN ('queued','sending')",
                ),
                cwd=self.repository_root,
            )
        )
        queue_output = self.runner.run(
            self._compose(
                "exec",
                "-T",
                "redis",
                "sh",
                "-ec",
                (
                    'exec redis-cli --user sms_broker --askpass --raw EVAL "$1" 0 '
                    "< /run/secrets/redis_broker_password"
                ),
                "sh",
                "return {redis.call('LLEN','realtime'),"
                "redis.call('LLEN','bulk'),redis.call('LLEN','callback')}",
            ),
            cwd=self.repository_root,
        )
        lines = queue_output.decode("ascii", errors="strict").splitlines()
        if len(lines) != 3 or any(not value.isdecimal() for value in lines):
            raise PerformanceFailure("PERF-03 probe returned invalid queue counts")
        return DrainSnapshot(
            active,
            dict(zip(("realtime", "bulk", "callback"), map(int, lines), strict=True)),
        )

    def worker_config(self) -> tuple[int, int]:
        output = self.runner.run(
            self._compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "sms_owner",
                "-d",
                "sms",
                "-Atc",
                "SELECT (SELECT value FROM sys_config WHERE key='vendor_qps') || '|' || "
                "(SELECT value FROM sys_config WHERE key='reserved_realtime_qps')",
            ),
            cwd=self.repository_root,
        )
        parts = output.decode("ascii", errors="strict").strip().split("|")
        if len(parts) != 2 or any(not value.isdecimal() for value in parts):
            raise PerformanceFailure("PERF-00 invalid vendor worker config")
        return int(parts[0]), int(parts[1])


@dataclass(frozen=True, slots=True)
class PerformanceConfig:
    acceptance_rps: int = 30
    acceptance_seconds: int = 60
    mixed_seconds: int = 60
    drain_timeout_s: int = 480
    cleanup_workers: int = 64

    def validate(self) -> None:
        if (
            min(
                self.acceptance_rps,
                self.acceptance_seconds,
                self.mixed_seconds,
                self.drain_timeout_s,
            )
            < 1
        ):
            raise ValueError("performance durations and rates must be positive")
        if not 1 <= self.cleanup_workers <= 64:
            raise ValueError("cleanup_workers must be between 1 and 64")


@dataclass(frozen=True, slots=True)
class PerformanceResult:
    acceptance_requests: int
    acceptance_p95_s: float
    verify_requests: int
    bulk_requests: int
    verify_p95_s: float
    event_loop_delay_seconds: float
    process_resident_memory_bytes: int
    database_connections_open: int
    database_connections_checked_out: int
    redis_connections_open: int
    redis_connections_in_use: int
    drain_seconds: float
    cancelled_scheduled_batches: int
    cleanup_seconds: float
    load_measurements: Mapping[str, LoadMeasurement]
    runtime_process_instance: str
    verify_acceptance_p95_s: float
    verify_post_accept_p95_s: float
    runtime_memory_semantics: str = "process_high_water_mark"


Scheduler = Callable[[Sequence[LoadEvent], Callable[[LoadEvent], Any]], list[Any]]


def _object(value: object, stage: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PerformanceFailure(f"{stage} returned an invalid JSON shape")
    return value


def _prometheus_value(document: str, sample: str) -> float:
    """精确样本只允许一个值，避免重复进程序列被悄悄取第一条。"""

    matches = [
        line[len(sample) + 1 :] for line in document.splitlines() if line.startswith(f"{sample} ")
    ]
    if len(matches) != 1:
        raise PerformanceFailure("PERF-02 runtime metrics are incomplete or ambiguous")
    try:
        value = float(matches[0])
    except ValueError:
        raise PerformanceFailure("PERF-02 runtime metrics are invalid") from None
    if not math.isfinite(value) or value < 0:
        raise PerformanceFailure("PERF-02 runtime metrics are invalid")
    return value


def _runtime_process_document(document: str) -> tuple[str, str]:
    """本次 scrape 只取一个显式进程实例；保留身份，绝不宣称全局采集。"""

    process_pattern = re.compile(r'process_instance="([a-zA-Z0-9_-]{1,64})"')
    instances: set[str] = set()
    normalized: list[str] = []
    for line in document.splitlines():
        if not line.startswith("sms_runtime_"):
            continue
        match = process_pattern.search(line)
        if match is None:
            raise PerformanceFailure("PERF-02 runtime sample lacks process identity")
        instances.add(match.group(1))
        line = line[: match.start()] + line[match.end() :]
        line = line.replace("{,", "{").replace(",}", "}").replace("{}", "")
        normalized.append(line)
    if len(instances) != 1:
        raise PerformanceFailure("PERF-02 runtime scrape must represent exactly one process")
    return "\n".join(normalized), instances.pop()


class PerformanceSuite:
    """以固定 mock 身份执行三阶段性能验收。"""

    def __init__(
        self,
        api: HttpClient,
        mock: HttpClient,
        probe: Probe,
        keys: Mapping[str, str],
        *,
        mock_password: str = "",
        metrics_token: str = "",
        config: PerformanceConfig | None = None,
        scheduler: Scheduler = run_open_loop,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        run_id: str | None = None,
    ) -> None:
        selected_config = config or PerformanceConfig()
        selected_config.validate()
        required = {"app-iam", "app-oa", "app-mkt"}
        if not required.issubset(keys) or any(not keys[name] for name in required):
            raise ValueError("performance API keys are incomplete")
        self.api = api
        self.mock = mock
        self.probe = probe
        self.keys = dict(keys)
        self.mock_password = mock_password
        self.metrics_token = metrics_token
        self.config = selected_config
        self.scheduler = scheduler
        self.clock = clock
        self.sleeper = sleeper
        self.run_id = run_id or uuid4().hex[:8]
        self.phone_run_bucket = (
            int.from_bytes(hashlib.sha256(self.run_id.encode()).digest()[:2], "big") % 10_000
        )
        self._scheduled_batches: list[tuple[str, str]] = []
        self._scheduled_lock = Lock()
        self._load_measurements: dict[str, LoadMeasurement] = {}
        self._runtime_process_instance = "unavailable"
        self._verify_acceptance: list[float] = []
        self._verify_post_accept: list[float] = []

    def _phone(self, namespace: int, index: int) -> str:
        tail = namespace * 20_000_000 + self.phone_run_bucket * 2_000 + index
        return f"188{tail:08d}"

    def _api_send(
        self,
        *,
        app: str,
        category: str,
        phone: str,
        content: str,
        index: int,
        scheduled_at: str | None = None,
    ) -> tuple[Mapping[str, Any], float]:
        payload: dict[str, object] = {
            "category": category,
            "mobiles": [phone],
            "content": content,
            "biz_id": f"pf-{self.run_id}-{index}",
        }
        if scheduled_at is not None:
            payload["scheduled_at"] = scheduled_at
        started = self.clock()
        response = self.api.request(
            "POST",
            "/api/v1/messages/send",
            payload=payload,
            headers={"X-Api-Key": self.keys[app]},
        )
        elapsed = self.clock() - started
        if response.status != 200:
            raise PerformanceFailure(f"PERF-01 API acceptance returned HTTP {response.status}")
        return _object(response.data, "PERF-01"), elapsed

    def _run_load[T](
        self,
        phase: str,
        events: Sequence[LoadEvent],
        handler: Callable[[LoadEvent], T],
    ) -> list[T]:
        """成功或失败均保存本阶段实际负载测量，供统一失败输出复用。"""

        try:
            result = self.scheduler(events, handler)
        except LoadGenerationFailure as error:
            self._load_measurements[phase] = error.measurement
            raise
        if isinstance(result, MeasuredResults):
            self._load_measurements[phase] = result.measurement
        return result

    def _phase_one(self) -> tuple[int, float]:
        scheduled_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        app_by_kind = {"verify": "app-iam", "notice": "app-oa", "market": "app-mkt"}
        content_by_kind = {
            "verify": "验证码123456",
            "notice": "性能验收通知",
            "market": "性能验收活动回T退订",
        }

        def accept(event: LoadEvent) -> float:
            app = app_by_kind[event.kind]
            data, elapsed = self._api_send(
                app=app,
                category=event.kind,
                phone=self._phone(1, event.index),
                content=content_by_kind[event.kind],
                index=event.index,
                scheduled_at=scheduled_at,
            )
            if data.get("status") != "scheduled":
                raise PerformanceFailure("PERF-01 future request was not scheduled")
            batch_no = data.get("batch_no")
            if not isinstance(batch_no, str) or not batch_no:
                raise PerformanceFailure("PERF-01 scheduled response omitted batch_no")
            with self._scheduled_lock:
                self._scheduled_batches.append((batch_no, app))
            return elapsed

        samples = self._run_load(
            "acceptance",
            build_acceptance_events(self.config.acceptance_rps, self.config.acceptance_seconds),
            accept,
        )
        p95 = percentile95(samples)
        if p95 >= ACCEPTANCE_P95_LIMIT_SECONDS:
            detail = acceptance_failure_detail(
                samples,
                rps=self.config.acceptance_rps,
            )
            raise PerformanceFailure(
                f"PERF-01 acceptance P95 {p95:.3f}s is not "
                f"<{ACCEPTANCE_P95_LIMIT_SECONDS:.3f}s; {detail}"
            )
        return len(samples), p95

    def _login_operator(self) -> str:
        response = self.api.request(
            "POST",
            "/api/v1/web/auth/login",
            payload={
                "provider_code": "ad",
                "username": "operator01",
                "password": self.mock_password,
                "session_mode": "refresh",
                "tab_id": TAB_ID,
            },
        )
        if response.status != 200:
            raise PerformanceFailure(f"PERF-02 operator login returned HTTP {response.status}")
        token = _object(response.data, "PERF-02").get("token")
        if not isinstance(token, str) or not token:
            raise PerformanceFailure("PERF-02 operator login omitted token")
        return token

    def _wait_mock_send(self, batch_no: str, accepted_at: float) -> float:
        prefix = batch_no[:24]
        deadline = accepted_at + 10
        while self.clock() <= deadline:
            response = self.mock.request("GET", "/_mock/state")
            if response.status != 200:
                raise PerformanceFailure("PERF-02 mock state unavailable")
            calls = _object(response.data, "PERF-02").get("send_calls")
            if not isinstance(calls, list):
                raise PerformanceFailure("PERF-02 mock state omitted send_calls")
            if any(
                isinstance(item, dict)
                and isinstance(item.get("customId"), str)
                and item["customId"].startswith(prefix)
                for item in calls
            ):
                return max(0.0, self.clock() - accepted_at)
            self.sleeper(0.05)
        raise PerformanceFailure("PERF-02 verify send was not observed within 10s")

    def _phase_two(self) -> tuple[int, int, float]:
        if not self.probe.snapshot().empty:
            raise PerformanceFailure("PERF-02 requires empty active batches and queues")
        reset = self.mock.request("POST", "/_mock/state", payload={"reset": True})
        if reset.status != 200:
            raise PerformanceFailure("PERF-02 mock reset failed")
        token = self._login_operator()

        def mixed(event: LoadEvent) -> float | None:
            if event.kind == "verify":
                request_started = self.clock()
                data, _elapsed = self._api_send(
                    app="app-iam",
                    category="verify",
                    phone=self._phone(2, event.index),
                    content="验证码654321",
                    index=100_000 + event.index,
                )
                if data.get("status") != "queued":
                    raise PerformanceFailure("PERF-02 verify request was not queued")
                batch_no = data.get("batch_no")
                if not isinstance(batch_no, str):
                    raise PerformanceFailure("PERF-02 verify response omitted batch_no")
                http_completed = self.clock()
                end_to_end = self._wait_mock_send(batch_no, request_started)
                with self._scheduled_lock:
                    self._verify_acceptance.append(http_completed - request_started)
                    self._verify_post_accept.append(
                        max(0.0, end_to_end - (http_completed - request_started))
                    )
                return end_to_end
            response = self.api.request(
                "POST",
                "/api/v1/web/messages/send",
                payload={
                    "category": "market",
                    "mobiles": [self._phone(3, event.index)],
                    "content": "性能验收活动回T退订",
                    "biz_id": f"pfw-{self.run_id}-{event.index}",
                    "consent_confirmed": True,
                    "is_test": True,
                    "remark": f"perf-{self.run_id}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if (
                response.status != 200
                or _object(response.data, "PERF-02").get("status") != "queued"
            ):
                raise PerformanceFailure(f"PERF-02 bulk acceptance returned HTTP {response.status}")
            return None

        results = self._run_load("mixed", build_mixed_events(self.config.mixed_seconds), mixed)
        samples = [value for value in results if isinstance(value, float)]
        p95 = percentile95(samples)
        if p95 >= 2:
            raise PerformanceFailure(f"PERF-02 verify P95 {p95:.3f}s is not <2.000s")
        return len(samples), len(results) - len(samples), p95

    def _runtime_metrics(self) -> tuple[float, int, int, int, int, int]:
        """在混合负载结束点记录 loop/连接池/RSS，作为性能报告证据。"""

        response = self.api.request(
            "GET",
            "/metrics",
            headers={"Authorization": f"Bearer {self.metrics_token}"},
        )
        if response.status != 200 or not isinstance(response.data, str):
            raise PerformanceFailure(f"PERF-02 runtime metrics returned HTTP {response.status}")
        document, self._runtime_process_instance = _runtime_process_document(response.data)
        values = (
            _prometheus_value(
                document,
                "sms_runtime_event_loop_delay_seconds",
            ),
            _prometheus_value(
                document,
                "sms_runtime_process_resident_memory_bytes",
            ),
            _prometheus_value(
                document,
                'sms_runtime_database_connections{state="open"}',
            ),
            _prometheus_value(
                document,
                'sms_runtime_database_connections{state="checked_out"}',
            ),
            _prometheus_value(
                document,
                'sms_runtime_redis_connections{state="open"}',
            ),
            _prometheus_value(
                document,
                'sms_runtime_redis_connections{state="in_use"}',
            ),
        )
        return (
            values[0],
            int(values[1]),
            int(values[2]),
            int(values[3]),
            int(values[4]),
            int(values[5]),
        )

    def _wait_for_empty(self, *, timeout_s: int, failure: str) -> float:
        started = self.clock()
        deadline = started + timeout_s
        while self.clock() <= deadline:
            if self.probe.snapshot().empty:
                return self.clock() - started
            self.sleeper(1)
        raise PerformanceFailure(f"{failure} within {timeout_s}s")

    def _phase_three(self) -> float:
        return self._wait_for_empty(
            timeout_s=self.config.drain_timeout_s,
            failure="PERF-03 queues did not drain",
        )

    def _cancel_scheduled_batch(self, batch: tuple[str, str]) -> int:
        batch_no, app = batch
        response = self.api.request(
            "POST",
            "/api/v1/messages/batches/" + urllib.parse.quote(batch_no, safe="") + "/cancel",
            headers={"X-Api-Key": self.keys[app]},
        )
        return response.status

    def _cleanup_scheduled_batches(self) -> tuple[int, float]:
        """通过正式取消接口回滚本轮 future 批次，只公开聚合失败数。"""

        with self._scheduled_lock:
            scheduled = tuple(dict.fromkeys(self._scheduled_batches))
        if not scheduled:
            return 0, 0.0
        started = self.clock()
        cancelled = 0
        failed: list[tuple[str, str]] = []
        failed_outcomes: Counter[str] = Counter()
        workers = min(self.config.cleanup_workers, len(scheduled))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._cancel_scheduled_batch, batch): batch for batch in scheduled
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    status = future.result()
                except Exception:
                    failed_outcomes["transport"] += 1
                    failed.append(batch)
                    continue
                if status == 200:
                    cancelled += 1
                else:
                    failed_outcomes[f"http_{status}"] += 1
                    failed.append(batch)
        cleanup_seconds = max(0.0, self.clock() - started)
        with self._scheduled_lock:
            self._scheduled_batches = failed
        if failed:
            raise PerformanceFailure(
                "PERF-04 scheduled cleanup failed: "
                f"failed={len(failed)} total={len(scheduled)} "
                f"cleanup_seconds={cleanup_seconds:.3f} "
                "outcomes="
                + ",".join(
                    f"{outcome}:{count}" for outcome, count in sorted(failed_outcomes.items())
                )
            )
        return cancelled, cleanup_seconds

    def run(self) -> PerformanceResult:
        phase_error: Exception | None = None
        measurements: (
            tuple[
                int,
                float,
                int,
                int,
                float,
                float,
                int,
                int,
                int,
                int,
                int,
                float,
            ]
            | None
        ) = None
        cleanup_error: Exception | None = None
        cancelled = 0
        cleanup_seconds = 0.0
        try:
            if self.probe.worker_config() != (5, 2):
                raise PerformanceFailure("PERF-00 requires vendor_qps=5 and reserved=2")
            self._wait_for_empty(
                timeout_s=min(self.config.drain_timeout_s, 120),
                failure="PERF-00 previous gate workload did not drain",
            )
            accepted, acceptance_p95 = self._phase_one()
            verify, bulk, verify_p95 = self._phase_two()
            runtime = self._runtime_metrics()
        except Exception as error:
            phase_error = error

        if phase_error is None:
            try:
                drain_seconds = self._phase_three()
                measurements = (
                    accepted,
                    acceptance_p95,
                    verify,
                    bulk,
                    verify_p95,
                    *runtime,
                    drain_seconds,
                )
            except Exception as error:
                phase_error = error
        try:
            cancelled, cleanup_seconds = self._cleanup_scheduled_batches()
        except Exception as error:
            cleanup_error = error
        if phase_error is not None or cleanup_error is not None:
            parts: list[str] = []
            for failure_part in (phase_error, cleanup_error):
                if failure_part is not None:
                    parts.append(
                        str(failure_part)
                        if isinstance(failure_part, PerformanceFailure)
                        else f"performance phase failed: {type(failure_part).__name__}"
                    )
            failure = PerformanceFailure("; ".join(parts))
            failure.load_measurements = dict(self._load_measurements)
            failure.cleanup_failed = cleanup_error is not None
            raise failure from None
        if measurements is None:
            raise PerformanceFailure("performance measurements are unavailable")
        return PerformanceResult(
            *measurements,
            cancelled,
            cleanup_seconds,
            dict(self._load_measurements),
            self._runtime_process_instance,
            percentile95(self._verify_acceptance),
            percentile95(self._verify_post_accept),
        )


def _load_keys(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("performance key file is unavailable or invalid") from error
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise ValueError("performance key file must be a string mapping")
    return {str(key): str(item) for key, item in value.items()}


def failure_payload(error: Exception) -> dict[str, object]:
    """统一输出所有已测聚合值，普通门禁/排空和清理失败也不能丢失负载窗口。"""

    payload: dict[str, object] = {"status": "failed", "error": str(error)}
    if isinstance(error, PerformanceFailure):
        payload["load_measurements"] = {
            phase: asdict(measurement) for phase, measurement in error.load_measurements.items()
        }
        payload["cleanup_failed"] = error.cleanup_failed
    if isinstance(error, LoadGenerationFailure):
        payload["load_measurement"] = asdict(error.measurement)
    return payload


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--mock-base", default="http://localhost:9028")
    parser.add_argument("--keys", type=Path, required=True)
    parser.add_argument(
        "--mock-password-file",
        type=Path,
        default=root / "deploy/secrets/ldap_bind_password",
    )
    parser.add_argument(
        "--metrics-token-file",
        type=Path,
        default=root / "deploy/secrets/metrics_scrape_token",
    )
    parser.add_argument("--compose-file", type=Path, default=root / "deploy/docker-compose.yml")
    parser.add_argument("--acceptance-rps", type=int, default=30)
    parser.add_argument("--acceptance-seconds", type=int, default=60)
    parser.add_argument("--mixed-seconds", type=int, default=60)
    parser.add_argument("--drain-timeout", type=int, default=480)
    parser.add_argument("--cleanup-workers", type=int, default=64)
    args = parser.parse_args()
    api_client = JsonHttpClient(args.base)
    mock_client = JsonHttpClient(args.mock_base)
    try:
        result = PerformanceSuite(
            api_client,
            mock_client,
            DrainProbe(
                CommandRunner(),
                compose_file=args.compose_file,
                repository_root=root,
            ),
            _load_keys(args.keys),
            mock_password=read_secret_file(
                args.mock_password_file,
                label="mock password",
            ),
            metrics_token=read_secret_file(
                args.metrics_token_file,
                label="metrics scrape token",
            ),
            config=PerformanceConfig(
                acceptance_rps=args.acceptance_rps,
                acceptance_seconds=args.acceptance_seconds,
                mixed_seconds=args.mixed_seconds,
                drain_timeout_s=args.drain_timeout,
                cleanup_workers=args.cleanup_workers,
            ),
        ).run()
    except (OSError, UnicodeError, ValueError, PerformanceFailure) as error:
        print(json.dumps(failure_payload(error), ensure_ascii=False))
        return 1
    finally:
        api_client.close()
        mock_client.close()
    print(
        json.dumps(
            {"status": "success", **asdict(result)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
