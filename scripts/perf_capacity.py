#!/usr/bin/env python3
"""候选版本容量门禁：万级单请求与频控主体场景的可重复报告。"""

from __future__ import annotations

import argparse
import json
import math
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
    measurement: Mapping[str, Any]
    reporter_commit: str

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
    validate_report_payload(report.as_json())
    p95_limit_ms = 3000 if report.recipient_count >= 10_000 else 2000
    if report.p95_ms >= p95_limit_ms:
        raise CapacityGateFailure(
            f"{report.scenario} P95 must be <{p95_limit_ms}ms (NFR-01)"
        )
    if report.measurement["rejected_count"] or report.measurement["failed_count"]:
        raise CapacityGateFailure(
            "capacity sample must have zero rejected or failed requests"
        )
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


def _number(value: object, field: str, *, integer: bool = False) -> float:
    """拒绝非有限值、负数和整数计数截断，错误不回显输入值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapacityGateFailure(f"invalid numeric metric: {field}")
    if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
        raise CapacityGateFailure(f"invalid numeric metric: {field}")
    return float(value)


def _sha(value: object, *, digest: bool = False) -> bool:
    pattern = r"sha256:[0-9a-f]{64}" if digest else r"[0-9a-f]{40}"
    return (
        isinstance(value, str)
        and re.fullmatch(pattern, value) is not None
        and set(value.removeprefix("sha256:")) != {"0"}
    )


def _safe_payload(
    payload: Mapping[str, object], *, path: tuple[str, ...] = (),
) -> None:
    """递归排除敏感字段；仅已声明且格式合法的摘要不按手机号扫描。"""

    commit_paths = {
        ("commit",), ("reporter_commit",),
        ("measurement", "commit"), ("measurement", "collector_commit"),
    }
    for key, value in payload.items():
        if key in FORBIDDEN_REPORT_KEYS:
            raise CapacityGateFailure("capacity report contains a forbidden key")
        if PHONE_RE.search(key):
            raise CapacityGateFailure("capacity report contains a phone number")
        field_path = (*path, key)
        if isinstance(value, dict):
            _safe_payload(value, path=field_path)
            continue
        if field_path in commit_paths and _sha(value):
            continue
        if (
            field_path == ("measurement", "config_sha256")
            or path in {("image_digests",), ("measurement", "image_digests")}
        ) and _sha(value, digest=True):
            continue
        if PHONE_RE.search(json.dumps(value, ensure_ascii=False)):
            raise CapacityGateFailure("capacity report contains a phone number")


def validate_report_payload(payload: Mapping[str, Any]) -> None:
    _safe_payload(payload)
    integers = {
        "recipient_count",
        "sql_count",
        "wal_bytes",
        "redis_ops",
        "worker_rss_bytes",
    }
    for field in REQUIRED_METRICS:
        if field not in payload:
            raise CapacityGateFailure(f"capacity report missing {field}")
        _number(payload[field], field, integer=field in integers)
    if payload["recipient_count"] != scenario_recipient_count(
        str(payload.get("scenario"))
    ):
        raise CapacityGateFailure("metrics recipient_count does not match scenario")
    if not payload["p50_ms"] <= payload["p95_ms"] <= payload["p99_ms"]:
        raise CapacityGateFailure("latency percentiles are out of order")
    if payload["pool_occupancy"] > 1:
        raise CapacityGateFailure("pool_occupancy must be a ratio in [0, 1]")
    evidence = payload.get("measurement")
    if not isinstance(evidence, dict):
        raise CapacityGateFailure("capacity report missing measurement evidence")
    required = {
        "source",
        "commit",
        "collector_commit",
        "image_digests",
        "config_sha256",
        "scenario",
        "started_at",
        "finished_at",
        "sample_count",
        "accepted_count",
        "rejected_count",
        "failed_count",
        "latency_unit",
        "request_count",
    }
    if set(evidence) != required:
        raise CapacityGateFailure(
            "measurement evidence fields are incomplete or unsupported"
        )
    if (
        evidence["source"] != "isolated-runtime-capture"
        or evidence["latency_unit"] != "ms"
    ):
        raise CapacityGateFailure("measurement source or latency unit is invalid")
    if (
        evidence["scenario"] != payload["scenario"]
        or evidence["commit"] != payload["commit"]
    ):
        raise CapacityGateFailure(
            "measurement scenario or commit does not match report"
        )
    if not _sha(evidence["commit"]) or not _sha(evidence["collector_commit"]):
        raise CapacityGateFailure("measurement commit is invalid")
    if not _sha(evidence["config_sha256"], digest=True):
        raise CapacityGateFailure("measurement configuration digest is invalid")
    images = evidence["image_digests"]
    if not isinstance(images, dict) or not images or images != payload["image_digests"]:
        raise CapacityGateFailure(
            "measurement image digests are missing or inconsistent"
        )
    if any(
        not isinstance(name, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) is None
        or not _sha(value, digest=True)
        for name, value in images.items()
    ):
        raise CapacityGateFailure("measurement image digests are invalid")
    for field in (
        "sample_count",
        "accepted_count",
        "rejected_count",
        "failed_count",
        "request_count",
    ):
        _number(evidence[field], field, integer=True)
    if (
        evidence["sample_count"] < 1
        or evidence["sample_count"] != evidence["accepted_count"]
    ):
        raise CapacityGateFailure(
            "measurement sample count must match accepted requests"
        )
    if evidence["request_count"] != sum(
        evidence[field]
        for field in ("accepted_count", "rejected_count", "failed_count")
    ):
        raise CapacityGateFailure("measurement request counts do not reconcile")
    try:
        started = datetime.fromisoformat(evidence["started_at"])
        finished = datetime.fromisoformat(evidence["finished_at"])
        if (
            started.utcoffset() is None
            or finished.utcoffset() is None
            or finished <= started
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise CapacityGateFailure(
            "measurement window must have aware, ordered timestamps"
        ) from None


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
    reporter_commit: str = "unavailable",
) -> CapacityReport:
    evidence = metrics.get("measurement")
    if not isinstance(evidence, dict):
        raise CapacityGateFailure("capacity report missing measurement evidence")
    images = (
        evidence.get("image_digests") if image_digests is None else dict(image_digests)
    )
    raw = {**metrics, "scenario": scenario, "commit": commit, "image_digests": images}
    validate_report_payload(raw)
    assert isinstance(images, dict)  # validated above
    report = CapacityReport(
        schema_version=2,
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
        image_digests=dict(images),
        measurement=dict(evidence),
        reporter_commit=reporter_commit,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    validate_report_payload(report.as_json())
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候选版本发送容量门禁")
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--metrics-json", required=True)
    parser.add_argument(
        "--expected-commit", required=True, help="independently recorded target SHA"
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    recipient_count = scenario_recipient_count(args.scenario)
    require_real_postgres(recipient_count)
    metrics = json.loads(Path(args.metrics_json).read_text(encoding="utf-8"))
    if (
        not isinstance(metrics, dict)
        or metrics.get("recipient_count") != recipient_count
    ):
        raise CapacityGateFailure("metrics recipient_count does not match scenario")
    report = build_report(
        scenario=args.scenario,
        metrics=metrics,
        commit=args.expected_commit,
        reporter_commit=git_commit(Path(__file__).resolve().parents[1]),
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
