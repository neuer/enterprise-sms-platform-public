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
        "P95<2000ms",
        "P95<2s",
        "480s",
        "perf_capacity.py",
        "perf_fault_matrix.py",
        "recipients_10000",
        "perf_vendor_scale.py",
        "200000",
        "Mock-only",
        "不进入 G2",
    ):
        assert token in document


def test_vendor_scale_asset_is_bounded_mock_only_and_splits_maxima() -> None:
    source = (ROOT / "scripts/perf_vendor_scale.py").read_text(encoding="utf-8")
    for token in (
        "ROW_BUDGET_TOTAL = 200_000",
        "PHASE_A_BATCHES = 8",
        "PHASE_A_PHONES = 10_000",
        "PHASE_B_VENDOR_QPS = 200",
        "PHASE_C_PHONES = 1_000",
        "PHASE_C_BATCH_SIZE = 1_000",
        "NOTICE_APP",
        "refuse_simultaneous_maxima",
        "validate_mock_base_url",
        "SCALE-00",
        "SCALE-04",
        "provision-notice-app",
        "NoticeAppProvisioner",
    ):
        assert token in source
    assert "verify_all.sh" not in source
    assert 'vendor_batch_size": "100000"' not in source
    assert re.search(r"(?<!\d)1\d{10}(?!\d)", source) is None
    assert "dev_iam_verify_key" not in source
