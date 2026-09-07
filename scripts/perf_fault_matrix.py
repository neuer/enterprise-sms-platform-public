#!/usr/bin/env python3
"""周期性故障矩阵：半成功、冷投影与 backlog 恢复的不可变断言。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class FaultCase:
    name: str
    invariant: str
    auto_resend: bool
    fail_closed: bool


FAULT_CASES = (
    FaultCase(
        "vendor_success_response_lost",
        "chunk 进入 uncertain，禁止自动重发或切换供应商",
        False,
        True,
    ),
    FaultCase(
        "vendor_success_mark_submitted_failed",
        "本地落库失败后进入 uncertain，gateway 只调用一次",
        False,
        True,
    ),
    FaultCase(
        "submitting_timeout_uncertain",
        "submitting 超时只转 uncertain，不得改回 pending",
        False,
        True,
    ),
    FaultCase(
        "redis_flush_projection_rebuild",
        "投影重建期间发送失败关闭，恢复后才重新受理",
        False,
        True,
    ),
    FaultCase(
        "worker_broker_backlog_drain",
        "恢复后按 child chunk / admission 有界排空",
        False,
        True,
    ),
)


def assert_matrix() -> None:
    names = [item.name for item in FAULT_CASES]
    if len(names) != len(set(names)):
        raise RuntimeError("fault case names must be unique")
    if any(item.auto_resend for item in FAULT_CASES):
        raise RuntimeError("fault matrix must not allow automatic resend")
    if not all(item.fail_closed for item in FAULT_CASES):
        raise RuntimeError("fault matrix must fail closed")


# 固定实际业务节点，禁止让调用者提供任意 shell/测试脚本冒充场景结果。
CASE_TESTS: dict[str, tuple[str, ...]] = {
    "vendor_success_response_lost": (
        "tests/test_send_worker.py::test_transport_error_becomes_uncertain_without_retry",
    ),
    "vendor_success_mark_submitted_failed": (
        "tests/test_send_worker.py::test_successful_vendor_call_with_writeback_failure_is_never_retried",
    ),
    "submitting_timeout_uncertain": (
        "tests/integration/test_perf_fault_recovery_postgres.py::test_submitting_timeout_is_uncertain_and_never_requeued",
    ),
    "redis_flush_projection_rebuild": (
        "tests/test_usage_ledger.py::test_redis_unavailable_is_fail_closed_before_reservation",
        "tests/test_usage_ledger.py::test_projection_rebuild_second_owner_is_rejected",
        "tests/test_usage_projection_bounded.py::test_rebuild_midpage_failure_keeps_not_ready_and_retry_is_absolute",
    ),
    "worker_broker_backlog_drain": (
        "tests/integration/test_perf_fault_recovery_postgres.py::test_same_outbox_event_recovers_after_broker_failure_and_executes_once",
        "tests/integration/test_outbox_postgres.py::test_outbox_concurrency_fencing_recovery_and_privileges",
        "tests/test_outbox.py::test_dispatcher_records_publish_success_and_broker_failure",
        "tests/test_outbox.py::test_duplicate_delivery_without_execution_claim_has_no_effect",
    ),
}


@dataclass(frozen=True, slots=True)
class TestResult:
    nodeid: str
    status: Literal["not_run", "failed", "passed"]
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    status: Literal["not_run", "failed", "passed"]
    expected_tests: int
    passed_tests: int
    tests: tuple[TestResult, ...] = ()


def _failure_type(case: ET.Element) -> str | None:
    """仅输出固定异常类型，不归档JUnit消息、trace或依赖返回值。"""

    failures = [*case.iter("failure"), *case.iter("error")]
    if not failures:
        return None
    # pytest JUnit不总提供type属性；从正文匹配已知类名但从不输出正文。
    for name in (
        "AssertionError",
        "IntegrityError",
        "OperationalError",
        "ProgrammingError",
        "TimeoutError",
        "ConnectionError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "KeyError",
        "ImportError",
        "ModuleNotFoundError",
        "CancelledError",
    ):
        if any(name in ET.tostring(item, encoding="unicode") for item in failures):
            return name
    return "TestFailure"


def collect_results(junit: Path, *, returncode: int) -> tuple[CaseResult, ...]:
    """只接受本次实际执行且无skip/failure的全部固定节点，不保留测试原始日志。"""

    observed: dict[str, list[TestResult]] = {}
    try:
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            name = case.get("name", "")
            module = case.get("classname", "").replace(".", "/") + ".py"
            key = f"{module}::{name}"
            test_status: Literal["not_run", "failed", "passed"] = (
                "failed"
                if list(case.iter("failure")) or list(case.iter("error"))
                else ("not_run" if list(case.iter("skipped")) else "passed")
            )
            observed.setdefault(key, []).append(TestResult(key, test_status, _failure_type(case)))
    except (OSError, ET.ParseError):
        observed = {}
    results: list[CaseResult] = []
    for name, nodes in CASE_TESTS.items():
        states = [observed.get(node, []) for node in nodes]
        passed = sum(len(items) == 1 and items[0].status == "passed" for items in states)
        status: Literal["not_run", "failed", "passed"] = "not_run"
        if returncode != 0 or any(
            any(item.status == "failed" for item in items) or len(items) > 1 for items in states
        ):
            status = "failed"
        elif passed == len(nodes):
            status = "passed" if returncode == 0 else "failed"
        tests = tuple(
            items[0]
            if len(items) == 1
            else TestResult(
                node,
                "failed" if items else "not_run",
                "DuplicateResult" if items else "MissingResult",
            )
            for node, items in zip(nodes, states, strict=True)
        )
        results.append(CaseResult(name, status, len(nodes), passed, tests))
    return tuple(results)


def execute_matrix(root: Path) -> tuple[CaseResult, ...]:
    """在现有pytest环境执行代码级故障回归；不创建服务，不主动连真实依赖。"""

    assert_matrix()
    with tempfile.TemporaryDirectory(prefix="sms-fault-results-") as directory:
        junit = Path(directory) / "results.xml"
        nodes = tuple(dict.fromkeys(node for items in CASE_TESTS.values() for node in items))
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-o",
                    "addopts=",
                    f"--junitxml={junit}",
                    *nodes,
                ],
                cwd=root / "backend",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=180,
                env={
                    # 固定矩阵不得继承外层定向pytest的-k/-x等选择器。
                    **{key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"},
                    "ENVIRONMENT": "test",
                    "DEBUG": "1",
                    "VENDOR_MOCK": "1",
                    "AUTH_MOCK": "1",
                },
            )
            code = completed.returncode
        except (OSError, subprocess.TimeoutExpired):
            code = 1
        return collect_results(junit, returncode=code)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="execute fixed code regression nodes"
    )
    args = parser.parse_args(argv)
    assert_matrix()
    results = (
        execute_matrix(Path(__file__).resolve().parents[1])
        if args.execute
        else tuple(CaseResult(name, "not_run", len(nodes), 0) for name, nodes in CASE_TESTS.items())
    )
    status = (
        "passed"
        if all(item.status == "passed" for item in results)
        else ("failed" if any(item.status == "failed" for item in results) else "not_run")
    )
    print(
        json.dumps(
            {
                "scope": "code_regression",
                "status": status,
                "cases": [asdict(item) for item in results],
            }
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
