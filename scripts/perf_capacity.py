#!/usr/bin/env python3
"""候选版本容量门禁：万级单请求与频控主体场景的可重复报告。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PHONE_RE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
FORBIDDEN_REPORT_KEYS = frozenset(
    {"mobiles", "content", "phone", "api_key", "secret", "task_id"}
)
REQUIRED_METRICS = (
    "recipient_count",
    "accepted_recipients_per_s",
    "segments_per_s",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "sql_count",
    "lock_wait_ms",
    "pool_occupancy",
    "wal_bytes",
    "redis_ops",
    "worker_rss_bytes",
    "outbox_oldest_age_s",
    "converge_s",
)
SCENARIOS = (
    "recipients_1",
    "recipients_100",
    "recipients_1000",
    "recipients_10000",
    "frequency_new_subjects",
    "frequency_hmac_alias_merge",
    "fairness_mixed_apps",
)


class CapacityGateFailure(RuntimeError):
    """容量门禁失败只公开场景与聚合指标。"""


@dataclass(frozen=True, slots=True)
class CapacityThresholds:
    p99_ms: float = 60_000
    sql_count_per_1000: int = 24
    converge_s: float = 480


@dataclass(frozen=True, slots=True)
class CapacityReport:
    schema_version: int
    commit: str
    environment: str
    scenario: str
    recipient_count: int
    accepted_recipients_per_s: float
    segments_per_s: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    sql_count: int
    lock_wait_ms: float
    pool_occupancy: float
    wal_bytes: int
    redis_ops: int
    worker_rss_bytes: int
    outbox_oldest_age_s: float
    converge_s: float
    image_digests: Mapping[str, str]
    generated_at: str

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        for key in FORBIDDEN_REPORT_KEYS:
            payload.pop(key, None)
        return payload


def scenario_recipient_count(name: str) -> int:
    if name == "recipients_1":
        return 1
    if name == "recipients_100":
        return 100
    if name == "recipients_1000":
        return 1_000
    if name == "recipients_10000":
        return 10_000
    if name in {"frequency_new_subjects", "frequency_hmac_alias_merge"}:
        return 1_000
    if name == "fairness_mixed_apps":
        return 100
    raise CapacityGateFailure(f"unknown scenario: {name}")


def require_real_postgres(recipient_count: int) -> None:
    if recipient_count < 10_000:
        return
    if os.environ.get("OUTBOX_POSTGRES_DSN"):
        return
    if os.environ.get("PERF_ALLOW_10K") == "1":
        return
    raise CapacityGateFailure(
        "10,000 recipients/request requires OUTBOX_POSTGRES_DSN or PERF_ALLOW_10K=1"
    )


def assert_thresholds(report: CapacityReport, thresholds: CapacityThresholds) -> None:
    if report.p99_ms > thresholds.p99_ms:
        raise CapacityGateFailure(
            f"{report.scenario} P99 {report.p99_ms}ms exceeds {thresholds.p99_ms}ms"
        )
    bound = max(8, (report.recipient_count // 1000) * thresholds.sql_count_per_1000)
    if report.recipient_count >= 1000 and report.sql_count > bound:
        raise CapacityGateFailure(
            f"{report.scenario} sql_count {report.sql_count} exceeds {bound}"
        )
    if report.converge_s > thresholds.converge_s:
        raise CapacityGateFailure(
            f"{report.scenario} converge {report.converge_s}s exceeds {thresholds.converge_s}s"
        )


def validate_report_payload(payload: Mapping[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if PHONE_RE.search(text) is not None:
        raise CapacityGateFailure("capacity report contains a phone number")
    for key in FORBIDDEN_REPORT_KEYS:
        if key in payload:
            raise CapacityGateFailure("capacity report contains a forbidden key")
    for field in REQUIRED_METRICS:
        if field not in payload:
            raise CapacityGateFailure(f"capacity report missing {field}")


def git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "0" * 40
    return result.stdout.strip() or ("0" * 40)


def build_report(
    *,
    scenario: str,
    metrics: Mapping[str, Any],
    commit: str,
    environment: str = "candidate",
    image_digests: Mapping[str, str] | None = None,
) -> CapacityReport:
    report = CapacityReport(
        schema_version=1,
        commit=commit,
        environment=environment,
        scenario=scenario,
        recipient_count=int(metrics["recipient_count"]),
        accepted_recipients_per_s=float(metrics["accepted_recipients_per_s"]),
        segments_per_s=float(metrics["segments_per_s"]),
        p50_ms=float(metrics["p50_ms"]),
        p95_ms=float(metrics["p95_ms"]),
        p99_ms=float(metrics["p99_ms"]),
        sql_count=int(metrics["sql_count"]),
        lock_wait_ms=float(metrics["lock_wait_ms"]),
        pool_occupancy=float(metrics["pool_occupancy"]),
        wal_bytes=int(metrics["wal_bytes"]),
        redis_ops=int(metrics["redis_ops"]),
        worker_rss_bytes=int(metrics["worker_rss_bytes"]),
        outbox_oldest_age_s=float(metrics["outbox_oldest_age_s"]),
        converge_s=float(metrics["converge_s"]),
        image_digests=dict(image_digests or {}),
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    validate_report_payload(report.as_json())
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候选版本发送容量门禁")
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--metrics-json", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    recipient_count = scenario_recipient_count(args.scenario)
    require_real_postgres(recipient_count)
    metrics = json.loads(Path(args.metrics_json).read_text(encoding="utf-8"))
    if int(metrics.get("recipient_count", -1)) != recipient_count:
        raise CapacityGateFailure("metrics recipient_count does not match scenario")
    report = build_report(
        scenario=args.scenario,
        metrics=metrics,
        commit=git_commit(Path(__file__).resolve().parents[1]),
    )
    assert_thresholds(report, CapacityThresholds())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.as_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
