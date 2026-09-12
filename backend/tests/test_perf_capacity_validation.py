"""容量工具必须拒绝可重复的假通过证据，无网络或压测。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from perf_capacity import (
    CapacityGateFailure,
    CapacityThresholds,
    assert_thresholds,
    build_report,
    main,
)

from tests.test_send_perf_gates import _metrics


def report(metrics: dict[str, Any], recipient_count: int = 1000) -> Any:
    return build_report(scenario=f"recipients_{recipient_count}", metrics=metrics, commit="c" * 40)


@pytest.mark.parametrize("count,limit", [(1, 2000), (1000, 2000), (10000, 3000)])
def test_nfr_strict_p95_boundary(count: int, limit: int) -> None:
    metrics = _metrics(count, p99_ms=59000)
    metrics["p95_ms"] = limit - 0.001
    assert_thresholds(report(metrics, count), CapacityThresholds())
    for latency in (limit, 20000):
        metrics["p95_ms"] = latency
        with pytest.raises(CapacityGateFailure, match="P95"):
            assert_thresholds(report(metrics, count), CapacityThresholds())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "0.9"])
def test_invalid_metrics_never_pass(value: object) -> None:
    metrics = _metrics(1000)
    metrics["p95_ms"] = value
    with pytest.raises(CapacityGateFailure):
        report(metrics)


@pytest.mark.parametrize(
    "key,value",
    [
        ("source", "synthetic"),
        ("latency_unit", "seconds"),
        ("image_digests", {}),
        ("sample_count", 0),
        ("accepted_count", 99),
        ("request_count", 0),
        ("config_sha256", ""),
        ("commit", "d" * 40),
        ("scenario", "recipients_1"),
        ("started_at", "2026-09-07T00:00:00"),
        ("finished_at", "2026-09-06T00:00:00Z"),
    ],
)
def test_invalid_measurement_identity_counts_and_window(key: str, value: object) -> None:
    metrics: dict[str, Any] = _metrics(1000)
    metrics["measurement"][key] = value
    with pytest.raises(CapacityGateFailure):
        report(metrics)


def test_rejected_or_failed_load_cannot_hide_in_accepted_latency() -> None:
    metrics: dict[str, Any] = _metrics(1000)
    metrics["measurement"].update(rejected_count=1, request_count=101)
    with pytest.raises(CapacityGateFailure, match="rejected"):
        assert_thresholds(report(metrics), CapacityThresholds())


def test_cli_preserves_measured_identity_and_separates_reporter(tmp_path: Path) -> None:
    source = tmp_path / "metrics.json"
    target = tmp_path / "report.json"
    source.write_text(json.dumps(_metrics(1000)))
    assert (
        main(
            [
                "--scenario",
                "recipients_1000",
                "--metrics-json",
                str(source),
                "--expected-commit",
                "c" * 40,
                "--output",
                str(target),
            ]
        )
        == 0
    )
    payload = json.loads(target.read_text())
    assert payload["commit"] == "c" * 40
    assert payload["image_digests"] == {"api": "sha256:" + "a" * 64}
    assert payload["reporter_commit"] != payload["commit"]
    assert payload["measurement"]["sample_count"] == 100


def test_cli_rejects_capture_from_another_target(tmp_path: Path) -> None:
    source = tmp_path / "metrics.json"
    target = tmp_path / "report.json"
    source.write_text(json.dumps(_metrics(1000)))
    with pytest.raises(CapacityGateFailure, match="commit does not match"):
        main(
            [
                "--scenario",
                "recipients_1000",
                "--expected-commit",
                "d" * 40,
                "--metrics-json",
                str(source),
                "--output",
                str(target),
            ]
        )
    assert not target.exists()


@pytest.mark.parametrize("field", ["commit", "reporter_commit"])
def test_valid_commit_with_phone_shaped_digits_is_not_pii(field: str) -> None:
    from perf_capacity import _safe_payload

    _safe_payload({field: "a" + "13800138000" + "b" * 28})


@pytest.mark.parametrize("payload", [
    {"reporter_commit": "13800138000"},
    {"note": "a" + "13800138000" + "b" * 28},
    {"other": {"commit": "a" + "13800138000" + "b" * 28}},
    {"phone": "a" * 40},
])
def test_digest_exception_keeps_phone_and_sensitive_fields_rejected(
    payload: dict[str, Any],
) -> None:
    from perf_capacity import _safe_payload

    with pytest.raises(CapacityGateFailure):
        _safe_payload(payload)
