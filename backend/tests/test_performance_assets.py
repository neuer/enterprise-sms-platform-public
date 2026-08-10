from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_locust_asset_is_bounded_to_100k_over_one_day_with_235_mix() -> None:
    source = (ROOT / "scripts/locustfile.py").read_text(encoding="utf-8")
    for token in (
        "TARGET_TOTAL = 100_000",
        "SECONDS_PER_DAY = 86_400",
        "constant_throughput(TARGET_TOTAL / SECONDS_PER_DAY)",
        "@task(2)",
        "@task(3)",
        "@task(5)",
        "PERF_KEYS_FILE",
        "environment.runner.quit()",
    ):
        assert token in source
    assert re.search(r"(?<!\d)1\d{10}(?!\d)", source) is None
    assert "dev_iam_verify_key" not in source


def test_performance_runbook_marks_full_day_execution_as_handover() -> None:
    document = (ROOT / "docs/PERFORMANCE.md").read_text(encoding="utf-8")
    for token in (
        "locust",
        "100000",
        "24",
        "PERF_KEYS_FILE",
        "-u 1",
        "[HANDOVER]",
        "perf_smoke.py",
        "P95<350ms",
        "P95<2s",
        "480s",
    ):
        assert token in document
