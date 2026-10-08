#!/usr/bin/env python3
"""共享测试机 Mock 量级压测：受理形状、厂商 QPS 与大分片分开打满。"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from perf_smoke import (
    TAB_ID,
    CommandRunner,
    DrainProbe,
    DrainSnapshot,
    HttpClient,
    JsonHttpClient,
    LoadEvent,
    PerformanceFailure,
    Probe,
    Runner,
    _object,
    percentile,
    percentile95,
    run_open_loop,
)
from runtime_credentials import read_secret_file

ROW_BUDGET_TOTAL = 200_000
PHASE_A_BATCHES = 8
PHASE_A_PHONES = 10_000
PHASE_A_P95_LIMIT_SECONDS = 3.0
PHASE_A_ROW_BUDGET = 80_000
PHASE_B_RATES: tuple[int, ...] = (20, 50, 100, 200)
PHASE_B_SECONDS = 20
PHASE_B_P95_LIMIT_SECONDS = 2.0
PHASE_B_ROW_BUDGET = 40_000
PHASE_B_VENDOR_QPS = 200
PHASE_B_RESERVED_QPS = 40
PHASE_C_BATCHES = 60
PHASE_C_PHONES = 1_000
PHASE_C_VENDOR_QPS = 20
PHASE_C_RESERVED_QPS = 8
PHASE_C_BATCH_SIZE = 1_000
PHASE_C_ROW_BUDGET = 60_000
PHASE_D_WAVE_PHONES = 10_000
GETREPORT_SAFE_PHONES = 15_000
MOCK_REPORT_READY_S = 2.0
ASSUMED_REPORT_POLL_S = 10
PHASE_D1_VENDOR_QPS = 20
PHASE_D1_RESERVED_QPS = 8
PHASE_D1_BATCH_SIZE = 500
PHASE_D1_BATCHES = 40
PHASE_D1_PHONES = 500
PHASE_D1_BATCH_RPS = 2.0
PHASE_D2_VENDOR_QPS = 20
PHASE_D2_RESERVED_QPS = 8
PHASE_D2_BATCH_SIZE = 1_000
PHASE_D2_BATCHES = 20
PHASE_D2_PHONES = 1_000
PHASE_D2_BATCH_RPS = 1.0
PHASE_D3_VENDOR_QPS = 40
PHASE_D3_RESERVED_QPS = 16
PHASE_D3_BATCH_SIZE = 500
PHASE_D3_BATCHES = 40
PHASE_D3_PHONES = 500
PHASE_D3_BATCH_RPS = 2.0
PHASE_D_ROW_BUDGET = 80_000
DRAIN_TIMEOUT_S = 480
RESTORE_VENDOR_QPS = 5
RESTORE_RESERVED_QPS = 2
RESTORE_BATCH_SIZE = 500
ALLOWED_MOCK_HOSTS = frozenset({"127.0.0.1", "localhost", "mock-vendor"})
ALLOWED_PHASES = frozenset({"a", "b", "c", "d"})
NOTICE_APP = "app-oa"
NOTICE_CONTENT = "量级压测通知"
RESTORE_KEYS = ("vendor_qps", "reserved_realtime_qps", "vendor_batch_size")
POLL_REPORT_TASK = "app.tasks.poll_report"
POLL_REPORT_QUEUE = "realtime-report"


def validate_mock_base_url(url: str) -> str:
    """只允许本机或 Compose Mock 源，拒绝真实厂商 HTTPS origin。"""

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        raise PerformanceFailure("SCALE-00 mock base must be plain HTTP")
    if parsed.username is not None or parsed.password is not None:
        raise PerformanceFailure("SCALE-00 mock base must not include credentials")
    host = (parsed.hostname or "").casefold()
    if host not in ALLOWED_MOCK_HOSTS:
        raise PerformanceFailure("SCALE-00 mock base host is not a local Mock endpoint")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PerformanceFailure("SCALE-00 mock base must be an origin only")
    return url.rstrip("/")


def parse_phases(raw: str) -> tuple[str, ...]:
    """允许在共享机续跑时跳过已消耗行预算的阶段，例如只跑 C 或只跑 D。"""

    phases = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not phases or any(phase not in ALLOWED_PHASES for phase in phases):
        raise PerformanceFailure("SCALE-00 phases must be a, b, c, and/or d")
    if len(phases) != len(set(phases)):
        raise PerformanceFailure("SCALE-00 phases must not repeat")
    return phases


def refuse_simultaneous_maxima(*, vendor_qps: int, vendor_batch_size: int) -> None:
    if vendor_qps >= PHASE_B_VENDOR_QPS and vendor_batch_size >= PHASE_C_BATCH_SIZE:
        raise PerformanceFailure("SCALE-00 refuses simultaneous 200 QPS and 1000-number chunks")


def mock_send_phone_counts(state: Mapping[str, Any]) -> list[int]:
    calls = state.get("send_calls")
    if not isinstance(calls, list):
        raise PerformanceFailure("SCALE-02 mock state omitted send_calls")
    counts: list[int] = []
    for item in calls:
        if not isinstance(item, dict):
            raise PerformanceFailure("SCALE-02 mock send_calls are invalid")
        mobile = item.get("mobile")
        if not isinstance(mobile, str) or not mobile:
            raise PerformanceFailure("SCALE-02 mock send omitted mobiles")
        counts.append(len(mobile.split(",")))
    return counts


def latency_detail(samples: Sequence[float]) -> str:
    return (
        f"p50={percentile(samples, 0.50):.3f}s "
        f"p90={percentile(samples, 0.90):.3f}s "
        f"p95={percentile95(samples):.3f}s "
        f"p99={percentile(samples, 0.99):.3f}s "
        f"max={max(samples):.3f}s"
    )


@dataclass(frozen=True, slots=True)
class VendorScaleConfig:
    total_row_budget: int = ROW_BUDGET_TOTAL
    phase_a_batches: int = PHASE_A_BATCHES
    phase_a_phones: int = PHASE_A_PHONES
    phase_a_p95_s: float = PHASE_A_P95_LIMIT_SECONDS
    phase_b_rates: tuple[int, ...] = PHASE_B_RATES
    phase_b_seconds: int = PHASE_B_SECONDS
    phase_b_p95_s: float = PHASE_B_P95_LIMIT_SECONDS
    measure_phase_b: bool = False
    phase_b_row_budget: int = PHASE_B_ROW_BUDGET
    phase_c_batches: int = PHASE_C_BATCHES
    phase_c_phones: int = PHASE_C_PHONES
    phase_c_row_budget: int = PHASE_C_ROW_BUDGET
    measure_throughput: bool = False
    throughput_fail_closed: bool = False
    phase_d_include_d3: bool = True
    phase_d_wave_phones: int = PHASE_D_WAVE_PHONES
    phase_d1_batches: int = PHASE_D1_BATCHES
    phase_d1_phones: int = PHASE_D1_PHONES
    phase_d1_vendor_qps: int = PHASE_D1_VENDOR_QPS
    phase_d1_reserved_qps: int = PHASE_D1_RESERVED_QPS
    phase_d1_batch_size: int = PHASE_D1_BATCH_SIZE
    phase_d1_batch_rps: float = PHASE_D1_BATCH_RPS
    phase_d2_batches: int = PHASE_D2_BATCHES
    phase_d2_phones: int = PHASE_D2_PHONES
    phase_d2_vendor_qps: int = PHASE_D2_VENDOR_QPS
    phase_d2_reserved_qps: int = PHASE_D2_RESERVED_QPS
    phase_d2_batch_size: int = PHASE_D2_BATCH_SIZE
    phase_d2_batch_rps: float = PHASE_D2_BATCH_RPS
    phase_d3_batches: int = PHASE_D3_BATCHES
    phase_d3_phones: int = PHASE_D3_PHONES
    phase_d3_vendor_qps: int = PHASE_D3_VENDOR_QPS
    phase_d3_reserved_qps: int = PHASE_D3_RESERVED_QPS
    phase_d3_batch_size: int = PHASE_D3_BATCH_SIZE
    phase_d3_batch_rps: float = PHASE_D3_BATCH_RPS
    phase_d_row_budget: int = PHASE_D_ROW_BUDGET
    drain_timeout_s: int = DRAIN_TIMEOUT_S
    phases: tuple[str, ...] = ("a", "b", "c")

    def enabled(self, phase: str) -> bool:
        return phase in self.phases

    def phase_d_stages(self) -> tuple[tuple[str, int, int, int, int, int, float], ...]:
        stages = (
            (
                "d1",
                self.phase_d1_vendor_qps,
                self.phase_d1_reserved_qps,
                self.phase_d1_batch_size,
                self.phase_d1_batches,
                self.phase_d1_phones,
                self.phase_d1_batch_rps,
            ),
            (
                "d2",
                self.phase_d2_vendor_qps,
                self.phase_d2_reserved_qps,
                self.phase_d2_batch_size,
                self.phase_d2_batches,
                self.phase_d2_phones,
                self.phase_d2_batch_rps,
            ),
        )
        if self.phase_d_include_d3:
            stages += (
                (
                    "d3",
                    self.phase_d3_vendor_qps,
                    self.phase_d3_reserved_qps,
                    self.phase_d3_batch_size,
                    self.phase_d3_batches,
                    self.phase_d3_phones,
                    self.phase_d3_batch_rps,
                ),
            )
        return stages

    def planned_phase_d_phones(self) -> int:
        return sum(
            batches * phones
            for _name, _qps, _res, _batch, batches, phones, _rps in self.phase_d_stages()
        )

    def validate(self) -> None:
        if not self.phases or any(phase not in ALLOWED_PHASES for phase in self.phases):
            raise ValueError("vendor-scale phases must be a, b, c, and/or d")
        if self.enabled("a") and self.enabled("c") and self.enabled("d"):
            raise ValueError("vendor-scale refuses A+C+D in one invocation")
        planned = 0
        if self.enabled("a"):
            planned += self.phase_a_batches * self.phase_a_phones
        if self.enabled("b"):
            planned += self.phase_b_row_budget
        if self.enabled("c"):
            planned += self.phase_c_batches * self.phase_c_phones
        if self.enabled("d"):
            planned += self.planned_phase_d_phones()
        if planned > self.total_row_budget:
            raise ValueError("vendor-scale phase budgets exceed the hard row cap")
        if self.phase_a_batches * self.phase_a_phones > PHASE_A_ROW_BUDGET:
            raise ValueError("phase A exceeds its row budget")
        if self.phase_c_batches * self.phase_c_phones > PHASE_C_ROW_BUDGET:
            raise ValueError("phase C exceeds its row budget")
        if self.planned_phase_d_phones() > self.phase_d_row_budget:
            raise ValueError("phase D exceeds its row budget")
        if min(self.phase_a_batches, self.phase_a_phones, self.drain_timeout_s) < 1:
            raise ValueError("vendor-scale durations and counts must be positive")
        if any(rate < 1 for rate in self.phase_b_rates) or self.phase_b_seconds < 1:
            raise ValueError("phase B rates and duration must be positive")
        if self.phase_b_p95_s <= 0:
            raise ValueError("phase B P95 limit must be positive")
        if self.enabled("d"):
            if self.phase_d_wave_phones < 1:
                raise ValueError("phase D wave phones must be positive")
            if self.phase_d2_vendor_qps >= PHASE_B_VENDOR_QPS:
                raise ValueError("phase D2 refuses vendor_qps>=200")
            if self.phase_d3_batch_size >= PHASE_C_BATCH_SIZE:
                raise ValueError("phase D3 refuses 1000-number chunks")
            for (
                name,
                qps,
                reserved,
                batch_size,
                batches,
                phones,
                batch_rps,
            ) in self.phase_d_stages():
                if min(qps, reserved, batch_size, batches, phones) < 1 or batch_rps <= 0:
                    raise ValueError(f"phase {name} durations and counts must be positive")
                if reserved >= qps:
                    raise ValueError(f"phase {name} reserved_realtime_qps must be < vendor_qps")
                refuse_simultaneous_maxima(vendor_qps=qps, vendor_batch_size=batch_size)
                wave_phones = batches * phones
                if wave_phones % 2 != 0:
                    raise ValueError(f"phase {name} must split into two equal waves")
                if self.phase_d_wave_phones * 2 != wave_phones:
                    raise ValueError(
                        f"phase {name} waves must be two packs of {self.phase_d_wave_phones}"
                    )
                if self.phase_d_wave_phones % phones != 0:
                    raise ValueError(f"phase {name} wave phones must divide by batch size")


@dataclass(frozen=True, slots=True)
class RampResult:
    target_rps: int
    accepted: int
    accept_p50_s: float
    accept_p90_s: float
    accept_p95_s: float
    accept_p99_s: float
    accept_max_s: float
    mock_sends: int
    mock_sends_per_s: float
    http_errors: int
    p95_gate_applied: bool


@dataclass(frozen=True, slots=True)
class ThroughputResult:
    name: str
    vendor_qps: int
    reserved_realtime_qps: int
    vendor_batch_size: int
    phones_accepted: int
    batches_accepted: int
    mock_sends: int
    phones_per_send_min: int
    phones_per_send_avg: float
    phones_per_send_max: int
    mock_phones_per_s: float
    inject_seconds: float
    drain_seconds: float
    delivered_increment: int
    sending_leftover: int
    uncertain: int
    phones_completed_per_s: float
    accept_p50_s: float
    accept_p90_s: float
    accept_p95_s: float
    accept_p99_s: float
    accept_max_s: float
    p95_gate_applied: bool
    pending_reports_peak: int
    official_poll_waves: int


@dataclass(frozen=True, slots=True)
class VendorScaleResult:
    rows_accepted: int
    phase_a_requests: int
    phase_a_p95_s: float
    cancelled_scheduled_batches: int
    phase_b_ramps: tuple[RampResult, ...]
    phase_c_requests: int
    phase_c_mock_sends: int
    phase_c_max_phones_per_send: int
    drain_seconds: float
    restored_vendor_qps: int
    restored_reserved_qps: int
    restored_batch_size: int
    phase_d_stages: tuple[ThroughputResult, ...]
    measure_throughput: bool


class RuntimeConfigClient:
    """通过管理 API 临时改发送参数，失败也必须回到 5/2/500。"""

    def __init__(self, api: HttpClient, token: str) -> None:
        self.api = api
        self._headers = {"Authorization": f"Bearer {token}"}
        self.original: dict[str, str] = {}

    def _auth(self) -> Mapping[str, str]:
        return self._headers

    def snapshot(self) -> dict[str, str]:
        response = self.api.request("GET", "/api/v1/web/admin/configs", headers=self._auth())
        if response.status != 200:
            raise PerformanceFailure(f"SCALE-00 config list returned HTTP {response.status}")
        rows = response.data
        if not isinstance(rows, list):
            raise PerformanceFailure("SCALE-00 config list is invalid")
        values: dict[str, str] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            value = item.get("value")
            if key in RESTORE_KEYS and isinstance(value, str):
                values[key] = value
        if set(values) != set(RESTORE_KEYS):
            raise PerformanceFailure("SCALE-00 sending config snapshot is incomplete")
        self.original = values
        return dict(values)

    def apply(self, updates: Mapping[str, str]) -> None:
        vendor_qps = int(updates.get("vendor_qps", self.original.get("vendor_qps", "5")))
        batch_size = int(
            updates.get("vendor_batch_size", self.original.get("vendor_batch_size", "500"))
        )
        refuse_simultaneous_maxima(vendor_qps=vendor_qps, vendor_batch_size=batch_size)
        response = self.api.request(
            "PUT",
            "/api/v1/web/admin/configs",
            payload={"items": [{"key": key, "value": value} for key, value in updates.items()]},
            headers=self._auth(),
        )
        if response.status != 200:
            raise PerformanceFailure(f"SCALE-00 config update returned HTTP {response.status}")

    def restore(self) -> None:
        self.apply(
            {
                "vendor_qps": str(RESTORE_VENDOR_QPS),
                "reserved_realtime_qps": str(RESTORE_RESERVED_QPS),
                "vendor_batch_size": str(RESTORE_BATCH_SIZE),
            }
        )


class SmsComposeDrainProbe:
    """通过 sms-compose exec 读 queued/sending 与三条队列，不回显主机。"""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    @staticmethod
    def _count(output: bytes) -> int:
        value = output.decode("ascii", errors="strict").strip()
        if not value.isdecimal():
            raise PerformanceFailure("SCALE-03 probe returned an invalid count")
        return int(value)

    def snapshot(self) -> DrainSnapshot:
        active = self._count(
            self.runner.run(
                [
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
                ]
            )
        )
        queue_output = self.runner.run(
            [
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
            ]
        )
        lines = queue_output.decode("ascii", errors="strict").splitlines()
        if len(lines) != 3 or any(not value.isdecimal() for value in lines):
            raise PerformanceFailure("SCALE-03 probe returned invalid queue counts")
        return DrainSnapshot(
            active,
            dict(zip(("realtime", "bulk", "callback"), map(int, lines), strict=True)),
        )

    def _sql(self, statement: str) -> str:
        output = self.runner.run(
            [
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
                statement,
            ]
        )
        return output.decode("ascii", errors="strict")

    def message_status_counts(self) -> tuple[int, int, int]:
        text = self._sql(
            "SELECT "
            "(SELECT count(*) FROM sms_message WHERE status='delivered')||' ' ||"
            "(SELECT count(*) FROM sms_message WHERE status IN ('pending','sent'))||' ' ||"
            "(SELECT count(*) FROM sms_chunk WHERE status='uncertain')"
        ).strip()
        parts = text.split()
        if len(parts) != 3 or any(not item.isdecimal() for item in parts):
            raise PerformanceFailure("SCALE-03 status counts are invalid")
        delivered, leftover, uncertain = (int(item) for item in parts)
        return delivered, leftover, uncertain

    def trigger_poll_report(self) -> None:
        """官方 poll_report 走独立 exec 进程，打 realtime-report，不占发送槽。"""

        try:
            self.runner.run(
                [
                    "exec",
                    "-T",
                    "worker-report",
                    "python",
                    "-c",
                    (
                        "from app.tasks import app;"
                        "app.send_task("
                        f"'{POLL_REPORT_TASK}',queue='{POLL_REPORT_QUEUE}'"
                        ")"
                    ),
                ]
            )
        except PerformanceFailure as error:
            raise PerformanceFailure("SCALE-03 official poll_report trigger failed") from error

    def worker_config(self) -> tuple[int, int]:
        return RESTORE_VENDOR_QPS, RESTORE_RESERVED_QPS


class QuietSshComposeRunner:
    """SSH 执行 sms-compose；失败只报聚合错误，不回显目标。"""

    def __init__(self, target: str, port: int) -> None:
        if not target or port < 1:
            raise PerformanceFailure("SCALE-03 remote drain target is incomplete")
        self._target = target
        self._port = port
        self._local = CommandRunner()

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        argv = [
            "ssh",
            "-p",
            str(self._port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=20",
            self._target,
            "sudo",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=development",
            "SMS_PLATFORM_ROOT=/opt/sms-platform",
            "/usr/local/sbin/sms-compose",
            *[str(item) for item in command],
        ]
        try:
            return self._local.run(argv, cwd=cwd)
        except PerformanceFailure as error:
            raise PerformanceFailure("SCALE-03 remote probe failed") from error


class LocalSmsComposeRunner:
    """在测试机本机通过 sms-compose exec 排空，不经 SSH。"""

    def __init__(self) -> None:
        self._local = CommandRunner()

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        argv = [
            "sudo",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=development",
            "SMS_PLATFORM_ROOT=/opt/sms-platform",
            "/usr/local/sbin/sms-compose",
            *[str(item) for item in command],
        ]
        try:
            return self._local.run(argv, cwd=cwd)
        except PerformanceFailure as error:
            raise PerformanceFailure("SCALE-03 local sms-compose probe failed") from error


def load_test_update_ssh(path: Path) -> tuple[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PerformanceFailure("SCALE-03 test-update env is unavailable") from error
    target = ""
    port = 22
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator == "" or key not in {
            "SMS_TEST_UPDATE_TARGET",
            "SMS_TEST_UPDATE_PORT",
            "SMS_VENDOR_LIVE_TEST_ORIGIN",
        }:
            raise PerformanceFailure("SCALE-03 test-update env has unexpected keys")
        if key == "SMS_TEST_UPDATE_TARGET":
            target = value
        elif key == "SMS_TEST_UPDATE_PORT":
            if not value.isdecimal():
                raise PerformanceFailure("SCALE-03 test-update port is invalid")
            port = int(value)
    if not target:
        raise PerformanceFailure("SCALE-03 test-update target is missing")
    return target, port


def _load_keys(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("performance key file is unavailable or invalid") from error
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise ValueError("performance key file must be a string mapping")
    return {str(key): str(item) for key, item in value.items()}


Scheduler = Callable[[Sequence[LoadEvent], Callable[[LoadEvent], Any]], list[Any]]


class SqlRuntimeConfigClient:
    """在无 Web 管理员会话时，用 sms-compose psql 改回 5/2/500。"""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.original: dict[str, str] = {}

    def _sql(self, statement: str) -> str:
        output = self.runner.run(
            [
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
                statement,
            ]
        )
        return output.decode("ascii", errors="strict")

    def snapshot(self) -> dict[str, str]:
        text = self._sql(
            "SELECT key||'='||value FROM sys_config WHERE key IN ("
            "'vendor_qps','reserved_realtime_qps','vendor_batch_size')"
        )
        values: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in RESTORE_KEYS:
                values[key] = value
        if set(values) != set(RESTORE_KEYS):
            raise PerformanceFailure("SCALE-00 sending config snapshot is incomplete")
        self.original = values
        return dict(values)

    def apply(self, updates: Mapping[str, str]) -> None:
        vendor_qps = int(updates.get("vendor_qps", self.original.get("vendor_qps", "5")))
        batch_size = int(
            updates.get("vendor_batch_size", self.original.get("vendor_batch_size", "500"))
        )
        refuse_simultaneous_maxima(vendor_qps=vendor_qps, vendor_batch_size=batch_size)
        for key, value in updates.items():
            if key not in RESTORE_KEYS or not value.isdigit():
                raise PerformanceFailure("SCALE-00 sql config update rejected")
            self._sql(
                "UPDATE sys_config SET value='"
                + value
                + "', updated_at=now() WHERE key='"
                + key
                + "'"
            )

    def restore(self) -> None:
        self.apply(
            {
                "vendor_qps": str(RESTORE_VENDOR_QPS),
                "reserved_realtime_qps": str(RESTORE_RESERVED_QPS),
                "vendor_batch_size": str(RESTORE_BATCH_SIZE),
            }
        )


class VendorScaleSuite:
    def __init__(
        self,
        api: HttpClient,
        mock: HttpClient,
        probe: Probe,
        keys: Mapping[str, str],
        config_client: RuntimeConfigClient | SqlRuntimeConfigClient,
        *,
        config: VendorScaleConfig | None = None,
        scheduler: Scheduler = run_open_loop,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        run_id: str | None = None,
        require_vendor_status: bool = True,
    ) -> None:
        selected = config or VendorScaleConfig()
        selected.validate()
        if not keys.get(NOTICE_APP):
            raise ValueError("vendor-scale requires the notice API key")
        self.api = api
        self.mock = mock
        self.probe = probe
        self.keys = dict(keys)
        self.config_client = config_client
        self.config = selected
        self.scheduler = scheduler
        self.clock = clock
        self.sleeper = sleeper
        self.run_id = run_id or uuid4().hex[:8]
        self.phone_run_bucket = (
            int.from_bytes(hashlib.sha256(self.run_id.encode()).digest()[:2], "big") % 90
        )
        self._scheduled: list[str] = []
        self._rows = 0
        self._phase_a_rows = 0
        self._phase_b_rows = 0
        self._phase_c_rows = 0
        self._phase_d_rows = 0
        self._d_phone_cursor = 0
        self.require_vendor_status = require_vendor_status
        self.phase_b_ramps_recorded: tuple[RampResult, ...] = ()
        self.phase_d_stages_recorded: tuple[ThroughputResult, ...] = ()

    def _charge(self, count: int, *, phase: str) -> None:
        if count < 1:
            raise PerformanceFailure("SCALE-00 send charged an empty recipient list")
        if self._rows + count > self.config.total_row_budget:
            raise PerformanceFailure("SCALE-00 row budget exhausted")
        budgets = {
            "a": (
                self._phase_a_rows,
                self.config.phase_a_batches * self.config.phase_a_phones,
            ),
            "b": (self._phase_b_rows, self.config.phase_b_row_budget),
            "c": (
                self._phase_c_rows,
                min(
                    self.config.phase_c_row_budget,
                    self.config.phase_c_batches * self.config.phase_c_phones,
                ),
            ),
            "d": (
                self._phase_d_rows,
                min(self.config.phase_d_row_budget, self.config.planned_phase_d_phones()),
            ),
        }
        used, limit = budgets[phase]
        if used + count > limit:
            raise PerformanceFailure(f"SCALE-00 phase {phase} row budget exhausted")
        self._rows += count
        if phase == "a":
            self._phase_a_rows += count
        elif phase == "b":
            self._phase_b_rows += count
        elif phase == "c":
            self._phase_c_rows += count
        else:
            self._phase_d_rows += count

    def _phone(self, namespace: int, index: int) -> str:
        tail = (self.phone_run_bucket * 1_000_000 + namespace * 100_000 + index) % 100_000_000
        return f"188{tail:08d}"

    def _phones(self, namespace: int, count: int) -> list[str]:
        return [self._phone(namespace, index) for index in range(count)]

    def _require_empty(self, *, timeout_s: int, failure: str) -> float:
        started = self.clock()
        deadline = started + timeout_s
        while self.clock() <= deadline:
            if self.probe.snapshot().empty:
                return max(0.0, self.clock() - started)
            self.sleeper(1)
        raise PerformanceFailure(f"{failure} within {timeout_s}s")

    def _reset_mock(self) -> None:
        response = self.mock.request("POST", "/_mock/state", payload={"reset": True})
        if response.status != 200:
            raise PerformanceFailure("SCALE-00 mock reset failed")

    def _mock_state(self) -> Mapping[str, Any]:
        response = self.mock.request("GET", "/_mock/state")
        if response.status != 200:
            raise PerformanceFailure("SCALE-02 mock state unavailable")
        return _object(response.data, "SCALE-02")

    def _preflight_vendor_status(self) -> None:
        if not isinstance(self.config_client, RuntimeConfigClient):
            raise PerformanceFailure("SCALE-00 vendor-test status requires admin HTTP")
        response = self.api.request(
            "GET",
            "/api/v1/web/admin/vendor-test/status",
            headers=self.config_client._auth(),
        )
        if response.status != 200:
            raise PerformanceFailure(f"SCALE-00 vendor-test status returned HTTP {response.status}")
        mode = _object(response.data, "SCALE-00").get("mode")
        if mode == "controlled":
            raise PerformanceFailure("SCALE-00 refuses a controlled live vendor")
        if mode == "blocked":
            raise PerformanceFailure("SCALE-00 vendor-test is blocked")
        if mode not in {"setup_required", "inactive"}:
            raise PerformanceFailure("SCALE-00 vendor-test mode is not Mock-safe")

    def _send_notice(
        self,
        *,
        mobiles: Sequence[str],
        index: int,
        scheduled_at: str | None,
        phase: str,
    ) -> tuple[Mapping[str, Any], float]:
        payload: dict[str, object] = {
            "category": "notice",
            "mobiles": list(mobiles),
            "content": NOTICE_CONTENT,
            "biz_id": f"vs{self.run_id}{index:06d}"[:32],
        }
        if scheduled_at is not None:
            payload["scheduled_at"] = scheduled_at
        started = self.clock()
        response = self.api.request(
            "POST",
            "/api/v1/messages/send",
            payload=payload,
            headers={"X-Api-Key": self.keys[NOTICE_APP]},
        )
        elapsed = self.clock() - started
        if response.status != 200:
            raise PerformanceFailure(f"SCALE-01 API acceptance returned HTTP {response.status}")
        self._charge(len(mobiles), phase=phase)
        return _object(response.data, "SCALE-01"), elapsed

    def _phase_a(self) -> tuple[int, float]:
        scheduled_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        samples: list[float] = []
        for index in range(self.config.phase_a_batches):
            data, elapsed = self._send_notice(
                mobiles=self._phones(1 + index, self.config.phase_a_phones),
                index=index,
                scheduled_at=scheduled_at,
                phase="a",
            )
            if data.get("status") != "scheduled":
                raise PerformanceFailure("SCALE-01 future request was not scheduled")
            batch_no = data.get("batch_no")
            if not isinstance(batch_no, str) or not batch_no:
                raise PerformanceFailure("SCALE-01 scheduled response omitted batch_no")
            self._scheduled.append(batch_no)
            samples.append(elapsed)
        p95 = percentile95(samples)
        if p95 >= self.config.phase_a_p95_s:
            raise PerformanceFailure(
                f"SCALE-01 10k-batch P95 {p95:.3f}s is not "
                f"<{self.config.phase_a_p95_s:.3f}s; {latency_detail(samples)}"
            )
        return len(samples), p95

    def _cancel_scheduled(self) -> int:
        failed = 0
        cancelled = 0
        for batch_no in tuple(self._scheduled):
            response = self.api.request(
                "POST",
                "/api/v1/messages/batches/" + urllib.parse.quote(batch_no, safe="") + "/cancel",
                headers={"X-Api-Key": self.keys[NOTICE_APP]},
            )
            if response.status == 200:
                cancelled += 1
            else:
                failed += 1
        if failed:
            raise PerformanceFailure(
                f"SCALE-04 scheduled cleanup failed: failed={failed} total={len(self._scheduled)}"
            )
        if cancelled != len(self._scheduled):
            raise PerformanceFailure("SCALE-04 cancelled count does not match scheduled accepts")
        self._scheduled.clear()
        return cancelled

    def _phase_b(self) -> tuple[RampResult, ...]:
        self.config_client.apply(
            {
                "vendor_qps": str(PHASE_B_VENDOR_QPS),
                "reserved_realtime_qps": str(PHASE_B_RESERVED_QPS),
            }
        )
        refuse_simultaneous_maxima(
            vendor_qps=PHASE_B_VENDOR_QPS,
            vendor_batch_size=int(self.config_client.original.get("vendor_batch_size", "500")),
        )
        results: list[RampResult] = []
        previous_sends = 0
        for rate in self.config.phase_b_rates:
            if self.config.measure_phase_b:
                # Keep reports so later ramps can drain; send counts are deltas.
                previous_sends = len(mock_send_phone_counts(self._mock_state()))
            else:
                self._reset_mock()
                previous_sends = 0
            events = [
                LoadEvent(index, index / rate, "notice")
                for index in range(rate * self.config.phase_b_seconds)
            ]

            def send_one(event: LoadEvent, *, current_rate: int = rate) -> float:
                _data, elapsed = self._send_notice(
                    mobiles=[self._phone(20 + current_rate, event.index)],
                    index=10_000 + current_rate * 10_000 + event.index,
                    scheduled_at=None,
                    phase="b",
                )
                return elapsed

            started = self.clock()
            samples = self.scheduler(events, send_one)
            elapsed = max(self.clock() - started, 1e-6)
            p50 = percentile(samples, 0.50)
            p90 = percentile(samples, 0.90)
            p95 = percentile95(samples)
            p99 = percentile(samples, 0.99)
            maximum = max(samples)
            gate_applied = not self.config.measure_phase_b
            if gate_applied and p95 >= self.config.phase_b_p95_s:
                raise PerformanceFailure(
                    f"SCALE-01 single-number P95 {p95:.3f}s is not "
                    f"<{self.config.phase_b_p95_s:.3f}s; {latency_detail(samples)}"
                )
            total_sends = len(mock_send_phone_counts(self._mock_state()))
            mock_sends = total_sends - previous_sends
            if mock_sends < 0:
                raise PerformanceFailure("SCALE-02 mock Send count moved backwards")
            allowed = PHASE_B_VENDOR_QPS * (self.config.phase_b_seconds + 1)
            if mock_sends > allowed:
                raise PerformanceFailure("SCALE-02 mock Send exceeded the 200 QPS ceiling")
            ramp = RampResult(
                target_rps=rate,
                accepted=len(samples),
                accept_p50_s=p50,
                accept_p90_s=p90,
                accept_p95_s=p95,
                accept_p99_s=p99,
                accept_max_s=maximum,
                mock_sends=mock_sends,
                mock_sends_per_s=mock_sends / elapsed,
                http_errors=0,
                p95_gate_applied=gate_applied,
            )
            results.append(ramp)
            self.phase_b_ramps_recorded = tuple(results)
            if self.config.measure_phase_b:
                print(
                    json.dumps({"event": "phase_b_ramp", **asdict(ramp)}, ensure_ascii=False),
                    flush=True,
                )
        return tuple(results)

    def _phase_c(self) -> tuple[int, int, int]:
        self.config_client.apply(
            {
                "vendor_qps": str(PHASE_C_VENDOR_QPS),
                "reserved_realtime_qps": str(PHASE_C_RESERVED_QPS),
                "vendor_batch_size": str(PHASE_C_BATCH_SIZE),
            }
        )
        self._reset_mock()
        for index in range(self.config.phase_c_batches):
            data, _elapsed = self._send_notice(
                mobiles=self._phones(40 + index, self.config.phase_c_phones),
                index=80_000 + index,
                scheduled_at=None,
                phase="c",
            )
            if data.get("status") not in {"queued", "sending"}:
                raise PerformanceFailure("SCALE-01 large-chunk request was not queued")
        deadline = self.clock() + self.config.drain_timeout_s
        counts: list[int] = []
        while self.clock() <= deadline:
            counts = mock_send_phone_counts(self._mock_state())
            if len(counts) >= self.config.phase_c_batches:
                break
            self.sleeper(1)
        if not counts:
            raise PerformanceFailure("SCALE-02 large-chunk mock Send was not observed")
        maximum = max(counts)
        if maximum > PHASE_C_BATCH_SIZE:
            raise PerformanceFailure("SCALE-02 vendor Send exceeded the 1000-number cap")
        return self.config.phase_c_batches, len(counts), maximum

    def _pending_reports(self) -> int:
        value = self._mock_state().get("pending_reports")
        if not isinstance(value, int) or value < 0:
            raise PerformanceFailure("SCALE-02 mock pending_reports is invalid")
        return value

    def _message_status_counts(self) -> tuple[int, int, int]:
        counts_fn = getattr(self.probe, "message_status_counts", None)
        if counts_fn is None:
            return (0, 0, 0)
        delivered, leftover, uncertain = counts_fn()
        if min(delivered, leftover, uncertain) < 0:
            raise PerformanceFailure("SCALE-03 status counts moved backwards")
        return int(delivered), int(leftover), int(uncertain)

    def _trigger_official_poll_report(self) -> None:
        trigger = getattr(self.probe, "trigger_poll_report", None)
        if trigger is None:
            raise PerformanceFailure("SCALE-03 official poll_report is unavailable")
        trigger()

    def _next_d_phones(self, count: int) -> list[str]:
        start = self._d_phone_cursor
        self._d_phone_cursor += count
        return [
            self._phone(80 + start // 100_000, start % 100_000 + index) for index in range(count)
        ]

    def _wait_wave_catchup(
        self,
        *,
        wave_phones: int,
        delivered_before: int,
        next_wave_phones: int,
    ) -> tuple[int, int]:
        started = self.clock()
        min_wait = MOCK_REPORT_READY_S + ASSUMED_REPORT_POLL_S
        deadline = started + self.config.drain_timeout_s
        peak = 0
        official_polls = 0
        last_poll_at = started - ASSUMED_REPORT_POLL_S
        iterations = 0
        max_iterations = max(1, int(self.config.drain_timeout_s) + 1)
        while iterations <= max_iterations and self.clock() <= deadline:
            pending = self._pending_reports()
            peak = max(peak, pending)
            delivered, leftover, _uncertain = self._message_status_counts()
            delivered_delta = max(0, delivered - delivered_before)
            elapsed = max(0.0, self.clock() - started)
            ready_for_next = (
                elapsed >= min_wait
                and pending + next_wave_phones <= GETREPORT_SAFE_PHONES
                and delivered_delta >= wave_phones
            )
            if ready_for_next:
                return peak, official_polls
            oversized = pending > GETREPORT_SAFE_PHONES
            stuck = leftover > 0 and pending == 0 and elapsed >= min_wait
            poll_due = self.clock() - last_poll_at >= ASSUMED_REPORT_POLL_S
            if (oversized or stuck or (elapsed >= min_wait and pending > 0)) and poll_due:
                self._trigger_official_poll_report()
                official_polls += 1
                last_poll_at = self.clock()
            iterations += 1
            self.sleeper(1)
        return peak, official_polls

    def _run_throughput_stage(
        self,
        *,
        name: str,
        vendor_qps: int,
        reserved_qps: int,
        batch_size: int,
        batches: int,
        phones: int,
        batch_rps: float,
        send_index_base: int,
    ) -> ThroughputResult:
        refuse_simultaneous_maxima(vendor_qps=vendor_qps, vendor_batch_size=batch_size)
        self.config_client.apply(
            {
                "vendor_qps": str(vendor_qps),
                "reserved_realtime_qps": str(reserved_qps),
                "vendor_batch_size": str(batch_size),
            }
        )
        refuse_simultaneous_maxima(vendor_qps=vendor_qps, vendor_batch_size=batch_size)
        delivered_before, _leftover_before, uncertain_before = self._message_status_counts()
        previous_sends = len(mock_send_phone_counts(self._mock_state()))
        wave_batches = batches // 2
        samples: list[float] = []
        inject_started = self.clock()
        pending_peak = 0
        official_polls = 0
        for wave_index in range(2):
            events = [
                LoadEvent(index, index / batch_rps, "notice") for index in range(wave_batches)
            ]

            def send_batch(
                event: LoadEvent,
                *,
                current_wave: int = wave_index,
            ) -> float:
                data, elapsed = self._send_notice(
                    mobiles=self._next_d_phones(phones),
                    index=send_index_base + current_wave * wave_batches + event.index,
                    scheduled_at=None,
                    phase="d",
                )
                if data.get("status") not in {"queued", "sending"}:
                    raise PerformanceFailure("SCALE-01 throughput request was not queued")
                return elapsed

            samples.extend(self.scheduler(events, send_batch))
            next_wave_phones = self.config.phase_d_wave_phones if wave_index == 0 else 0
            wave_peak, wave_polls = self._wait_wave_catchup(
                wave_phones=self.config.phase_d_wave_phones,
                delivered_before=delivered_before + wave_index * self.config.phase_d_wave_phones,
                next_wave_phones=next_wave_phones,
            )
            pending_peak = max(pending_peak, wave_peak)
            official_polls += wave_polls
        inject_seconds = max(self.clock() - inject_started, 1e-6)
        send_counts = mock_send_phone_counts(self._mock_state())
        stage_counts = send_counts[previous_sends:]
        mock_sends = len(stage_counts)
        if mock_sends < 0:
            raise PerformanceFailure("SCALE-02 mock Send count moved backwards")
        phones_in_sends = sum(stage_counts)
        phones_min = min(stage_counts) if stage_counts else 0
        phones_max = max(stage_counts) if stage_counts else 0
        phones_avg = phones_in_sends / mock_sends if mock_sends else 0.0
        if phones_max > PHASE_C_BATCH_SIZE:
            raise PerformanceFailure("SCALE-02 vendor Send exceeded the 1000-number cap")
        drain_seconds = self._require_empty(
            timeout_s=self.config.drain_timeout_s,
            failure=f"SCALE-03 phase {name} queues did not drain",
        )
        delivered_after, leftover, uncertain_after = self._message_status_counts()
        if leftover > 0 and self._pending_reports() == 0:
            self._trigger_official_poll_report()
            official_polls += 1
            drain_seconds += self._require_empty(
                timeout_s=min(self.config.drain_timeout_s, 120),
                failure=f"SCALE-03 phase {name} leftover did not drain after poll",
            )
            delivered_after, leftover, uncertain_after = self._message_status_counts()
        delivered_increment = max(0, delivered_after - delivered_before)
        uncertain = max(0, uncertain_after - uncertain_before)
        if leftover > 0 and self._pending_reports() == 0:
            self._trigger_official_poll_report()
            official_polls += 1
            delivered_after, leftover, uncertain_after = self._message_status_counts()
            delivered_increment = max(0, delivered_after - delivered_before)
            uncertain = max(0, uncertain_after - uncertain_before)
        completed_window = max(inject_seconds + drain_seconds, 1e-6)
        result = ThroughputResult(
            name=name,
            vendor_qps=vendor_qps,
            reserved_realtime_qps=reserved_qps,
            vendor_batch_size=batch_size,
            phones_accepted=batches * phones,
            batches_accepted=len(samples),
            mock_sends=mock_sends,
            phones_per_send_min=phones_min,
            phones_per_send_avg=phones_avg,
            phones_per_send_max=phones_max,
            mock_phones_per_s=phones_in_sends / inject_seconds,
            inject_seconds=inject_seconds,
            drain_seconds=drain_seconds,
            delivered_increment=delivered_increment,
            sending_leftover=leftover,
            uncertain=uncertain,
            phones_completed_per_s=delivered_increment / completed_window,
            accept_p50_s=percentile(samples, 0.50),
            accept_p90_s=percentile(samples, 0.90),
            accept_p95_s=percentile95(samples),
            accept_p99_s=percentile(samples, 0.99),
            accept_max_s=max(samples),
            p95_gate_applied=False,
            pending_reports_peak=pending_peak,
            official_poll_waves=official_polls,
        )
        if self.config.measure_throughput or self.config.enabled("d"):
            print(
                json.dumps({"event": "phase_d_stage", **asdict(result)}, ensure_ascii=False),
                flush=True,
            )
        if self.config.throughput_fail_closed:
            if uncertain > 0:
                raise PerformanceFailure(f"SCALE-02 phase {name} introduced uncertain chunks")
            if leftover > 0:
                raise PerformanceFailure(f"SCALE-03 phase {name} leftover sending after drain")
        snapshot = self.probe.snapshot()
        if snapshot.empty:
            self._reset_mock()
        return result

    def _phase_d(self) -> tuple[ThroughputResult, ...]:
        results: list[ThroughputResult] = []
        send_index_base = 200_000
        for (
            name,
            qps,
            reserved,
            batch_size,
            batches,
            phones,
            batch_rps,
        ) in self.config.phase_d_stages():
            stage = self._run_throughput_stage(
                name=name,
                vendor_qps=qps,
                reserved_qps=reserved,
                batch_size=batch_size,
                batches=batches,
                phones=phones,
                batch_rps=batch_rps,
                send_index_base=send_index_base,
            )
            results.append(stage)
            self.phase_d_stages_recorded = tuple(results)
            send_index_base += batches
        return tuple(results)

    def run(self) -> VendorScaleResult:
        phase_error: Exception | None = None
        measurements: (
            tuple[
                int,
                float,
                tuple[RampResult, ...],
                int,
                int,
                int,
                float,
                tuple[ThroughputResult, ...],
            ]
            | None
        ) = None
        cancelled = 0
        cleanup_error: PerformanceFailure | None = None
        try:
            if self.require_vendor_status:
                self._preflight_vendor_status()
            self.config_client.snapshot()
            self._require_empty(
                timeout_s=min(self.config.drain_timeout_s, 120),
                failure="SCALE-00 previous workload did not drain",
            )
            self._reset_mock()
            accepted_a, p95_a = (0, 0.0)
            if self.config.enabled("a"):
                accepted_a, p95_a = self._phase_a()
                cancelled = self._cancel_scheduled()
            ramps: tuple[RampResult, ...] = ()
            if self.config.enabled("b"):
                ramps = self._phase_b()
            phase_c_requests, mock_sends, max_phones = (0, 0, 0)
            if self.config.enabled("c"):
                phase_c_requests, mock_sends, max_phones = self._phase_c()
            phase_d_stages: tuple[ThroughputResult, ...] = ()
            if self.config.enabled("d"):
                phase_d_stages = self._phase_d()
            drain_seconds = self._require_empty(
                timeout_s=self.config.drain_timeout_s,
                failure="SCALE-03 queues did not drain",
            )
            measurements = (
                accepted_a,
                p95_a,
                ramps,
                phase_c_requests,
                mock_sends,
                max_phones,
                drain_seconds,
                phase_d_stages,
            )
        except Exception as error:
            phase_error = error
        try:
            if self._scheduled:
                cancelled = self._cancel_scheduled()
        except PerformanceFailure as error:
            cleanup_error = error
        restore_error: PerformanceFailure | None = None
        try:
            self.config_client.restore()
        except PerformanceFailure as error:
            restore_error = error
        leftover_error: PerformanceFailure | None = None
        leftover_empty = False
        try:
            self._require_empty(
                timeout_s=self.config.drain_timeout_s,
                failure="SCALE-03 leftover queues did not drain",
            )
            leftover_empty = True
        except PerformanceFailure as error:
            leftover_error = error
        if leftover_empty:
            try:
                self._reset_mock()
            except PerformanceFailure as error:
                restore_error = restore_error or error
        elif leftover_error is None:
            leftover_error = PerformanceFailure(
                "SCALE-03 leftover queues did not drain; mock reset skipped"
            )
        if phase_error is not None:
            extras = [
                item for item in (cleanup_error, leftover_error, restore_error) if item is not None
            ]
            if extras:
                summary = (
                    str(phase_error)
                    if isinstance(phase_error, PerformanceFailure)
                    else f"vendor-scale phase failed: {type(phase_error).__name__}"
                )
                raise PerformanceFailure(
                    summary + "; " + "; ".join(str(item) for item in extras)
                ) from None
            raise phase_error
        if cleanup_error is not None:
            raise cleanup_error
        if leftover_error is not None:
            raise leftover_error
        if restore_error is not None:
            raise restore_error
        if measurements is None:
            raise PerformanceFailure("vendor-scale measurements are unavailable")
        return VendorScaleResult(
            rows_accepted=self._rows,
            phase_a_requests=measurements[0],
            phase_a_p95_s=measurements[1],
            cancelled_scheduled_batches=cancelled,
            phase_b_ramps=measurements[2],
            phase_c_requests=measurements[3],
            phase_c_mock_sends=measurements[4],
            phase_c_max_phones_per_send=measurements[5],
            drain_seconds=measurements[6],
            restored_vendor_qps=RESTORE_VENDOR_QPS,
            restored_reserved_qps=RESTORE_RESERVED_QPS,
            restored_batch_size=RESTORE_BATCH_SIZE,
            phase_d_stages=measurements[7],
            measure_throughput=self.config.measure_throughput or self.config.enabled("d"),
        )


class NoticeAppProvisioner:
    """创建高配额 notice 应用，结束后停用；不把 Key 写入报告。"""

    def __init__(self, api: HttpClient, token: str) -> None:
        self.api = api
        self._headers = {"Authorization": f"Bearer {token}"}
        self.app_id: int | None = None

    def create(self, *, sign_name: str) -> dict[str, str]:
        if not sign_name:
            raise PerformanceFailure("SCALE-00 notice app requires an approved sign")
        response = self.api.request(
            "POST",
            "/api/v1/web/admin/apps",
            payload={
                "name": f"scale-mock-{uuid4().hex[:8]}",
                "dept": "平台技术部",
                "allowed_categories": ["notice"],
                "default_sign": sign_name,
                "daily_quota": 0,
                "rate_limit_per_min": 60_000,
                "blacklist_check": False,
            },
            headers=self._headers,
        )
        if response.status != 200:
            raise PerformanceFailure(f"SCALE-00 notice app create returned HTTP {response.status}")
        data = _object(response.data, "SCALE-00")
        app_id = data.get("id")
        api_key = data.get("api_key")
        if not isinstance(app_id, int) or not isinstance(api_key, str) or not api_key:
            raise PerformanceFailure("SCALE-00 notice app create omitted credentials")
        self.app_id = app_id
        return {NOTICE_APP: api_key}

    def disable(self) -> None:
        if self.app_id is None:
            return
        response = self.api.request(
            "GET",
            f"/api/v1/web/admin/apps/{self.app_id}",
            headers=self._headers,
        )
        if response.status != 200:
            raise PerformanceFailure(f"SCALE-00 notice app lookup returned HTTP {response.status}")
        current = _object(response.data, "SCALE-00")
        categories = current.get("allowed_categories")
        if not isinstance(categories, list):
            raise PerformanceFailure("SCALE-00 notice app categories are invalid")
        payload = {
            "dept": current.get("dept") or "平台技术部",
            "allowed_categories": categories,
            "default_sign": current.get("default_sign"),
            "daily_quota": current.get("daily_quota") or 0,
            "rate_limit_per_min": current.get("rate_limit_per_min") or 60,
            "blacklist_check": bool(current.get("blacklist_check")),
            "allowed_ips": current.get("allowed_ips") or [],
            "callback_url": current.get("callback_url"),
            "callback_report_enabled": bool(current.get("callback_report_enabled")),
            "status": 0,
        }
        update = self.api.request(
            "PUT",
            f"/api/v1/web/admin/apps/{self.app_id}",
            payload=payload,
            headers=self._headers,
        )
        if update.status != 200:
            raise PerformanceFailure(f"SCALE-00 notice app disable returned HTTP {update.status}")


def _login_admin(
    api: HttpClient,
    password: str,
    *,
    username: str = "admin01",
    provider_code: str = "ad",
) -> str:
    response = api.request(
        "POST",
        "/api/v1/web/auth/login",
        payload={
            "provider_code": provider_code,
            "username": username,
            "password": password,
            "tab_id": TAB_ID,
        },
    )
    if response.status != 200:
        raise PerformanceFailure(f"SCALE-00 admin login returned HTTP {response.status}")
    token = _object(response.data, "SCALE-00").get("token")
    if not isinstance(token, str) or not token:
        raise PerformanceFailure("SCALE-00 admin login omitted token")
    return token


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--mock-base", default="http://127.0.0.1:9028")
    parser.add_argument("--keys", type=Path)
    parser.add_argument("--provision-notice-app", action="store_true")
    parser.add_argument("--sign-name", default="")
    parser.add_argument("--admin-username", default="admin01")
    parser.add_argument("--admin-provider", default="ad")
    parser.add_argument("--config-via", choices=("http", "sql"), default="http")
    parser.add_argument(
        "--admin-password-file",
        type=Path,
        default=root / "deploy/secrets/ldap_bind_password",
    )
    parser.add_argument("--compose-file", type=Path, default=root / "deploy/docker-compose.yml")
    parser.add_argument(
        "--drain-via",
        choices=("compose", "ssh", "sms-compose"),
        default="compose",
    )
    parser.add_argument(
        "--test-update-env",
        type=Path,
        default=root / ".env.test-update",
    )
    parser.add_argument("--drain-timeout", type=int, default=DRAIN_TIMEOUT_S)
    parser.add_argument("--phases", default="a,b,c")
    parser.add_argument(
        "--phase-b-p95-s",
        type=float,
        default=PHASE_B_P95_LIMIT_SECONDS,
        help="Phase B accept P95 fail-close threshold in seconds (default 2.0)",
    )
    parser.add_argument(
        "--measure-phase-b",
        action="store_true",
        help="Record Phase B latency and mock Send/s without fail-closing on P95",
    )
    parser.add_argument(
        "--measure-throughput",
        action="store_true",
        help="Record Phase D number-throughput metrics without a 2s single-number P95 gate",
    )
    parser.add_argument(
        "--throughput-fail-closed",
        action="store_true",
        help="Fail Phase D when uncertain>0 or leftover sending remains after drain",
    )
    parser.add_argument(
        "--skip-d3",
        action="store_true",
        help="Run D1+D2 only (40k). Default includes D3 (60k).",
    )
    args = parser.parse_args()
    try:
        mock_base = validate_mock_base_url(args.mock_base)
    except PerformanceFailure as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    api_client = JsonHttpClient(args.base, timeout_s=300)
    mock_client = JsonHttpClient(mock_base, timeout_s=30)
    suite: VendorScaleSuite | None = None
    try:
        if args.drain_via == "ssh":
            target, port = load_test_update_ssh(args.test_update_env)
            probe: Probe = SmsComposeDrainProbe(QuietSshComposeRunner(target, port))
        elif args.drain_via == "sms-compose":
            probe = SmsComposeDrainProbe(LocalSmsComposeRunner())
        else:
            probe = DrainProbe(
                CommandRunner(),
                compose_file=args.compose_file,
                repository_root=root,
            )
        provisioner: NoticeAppProvisioner | None = None
        if args.config_via == "sql":
            if args.provision_notice_app:
                raise ValueError("sql config mode cannot provision a web admin app")
            if args.keys is None:
                raise ValueError("sql config mode requires --keys")
            keys = _load_keys(args.keys)
            config_client: RuntimeConfigClient | SqlRuntimeConfigClient
            if args.drain_via == "ssh":
                target, port = load_test_update_ssh(args.test_update_env)
                config_client = SqlRuntimeConfigClient(QuietSshComposeRunner(target, port))
            elif args.drain_via == "sms-compose":
                config_client = SqlRuntimeConfigClient(LocalSmsComposeRunner())
            else:
                raise ValueError("sql config mode requires --drain-via ssh or sms-compose")
            require_vendor_status = False
        else:
            token = _login_admin(
                api_client,
                read_secret_file(args.admin_password_file, label="admin password"),
                username=args.admin_username,
                provider_code=args.admin_provider,
            )
            if args.provision_notice_app:
                if args.keys is not None:
                    raise ValueError("provision-notice-app cannot be combined with --keys")
                provisioner = NoticeAppProvisioner(api_client, token)
                keys = provisioner.create(sign_name=args.sign_name)
            elif args.keys is not None:
                keys = _load_keys(args.keys)
            else:
                raise ValueError("vendor-scale requires --keys or --provision-notice-app")
            config_client = RuntimeConfigClient(api_client, token)
            require_vendor_status = True
        try:
            suite = VendorScaleSuite(
                api_client,
                mock_client,
                probe,
                keys,
                config_client,
                config=VendorScaleConfig(
                    drain_timeout_s=args.drain_timeout,
                    phases=parse_phases(args.phases),
                    phase_b_p95_s=args.phase_b_p95_s,
                    measure_phase_b=args.measure_phase_b,
                    measure_throughput=args.measure_throughput or "d" in parse_phases(args.phases),
                    throughput_fail_closed=args.throughput_fail_closed,
                    phase_d_include_d3=not args.skip_d3,
                ),
                require_vendor_status=require_vendor_status,
            )
            result = suite.run()
        finally:
            if provisioner is not None:
                provisioner.disable()
    except (OSError, UnicodeError, ValueError, PerformanceFailure) as error:
        payload: dict[str, object] = {"status": "failed", "error": str(error)}
        recorded = getattr(suite, "phase_b_ramps_recorded", ()) if suite is not None else ()
        if recorded:
            payload["phase_b_ramps"] = [asdict(item) for item in recorded]
        recorded_d = getattr(suite, "phase_d_stages_recorded", ()) if suite is not None else ()
        if recorded_d:
            payload["phase_d_stages"] = [asdict(item) for item in recorded_d]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        api_client.close()
        mock_client.close()
    payload = asdict(result)
    print(json.dumps({"status": "success", **payload}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
