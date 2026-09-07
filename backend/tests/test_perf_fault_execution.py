"""故障结果必须来自执行，缺失、跳过与异常退出不得通过。"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from perf_fault_matrix import CASE_TESTS, collect_results, execute_matrix, main  # noqa: E402


def _junit(path: Path, *, broken: str | None = None, skipped: str | None = None) -> None:
    suite = ET.Element("testsuite")
    for nodes in CASE_TESTS.values():
        for node in nodes:
            module, name = node.split("::")
            case = ET.SubElement(
                suite, "testcase", classname=module[:-3].replace("/", "."), name=name
            )
            if node == broken:
                ET.SubElement(case, "failure")
            if node == skipped:
                ET.SubElement(case, "skipped")
    ET.ElementTree(suite).write(path)


def test_matrix_defaults_to_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    assert '"status": "not_run"' in capsys.readouterr().out


def test_result_violation_skip_missing_and_command_failure_reject(tmp_path: Path) -> None:
    junit = tmp_path / "results.xml"
    first = next(iter(CASE_TESTS.values()))[0]
    assert all(item.status == "not_run" for item in collect_results(junit, returncode=0))
    _junit(junit)
    assert all(item.status == "passed" for item in collect_results(junit, returncode=0))
    assert all(item.status == "failed" for item in collect_results(junit, returncode=1))
    _junit(junit, broken=first)
    assert collect_results(junit, returncode=1)[0].status == "failed"
    _junit(junit, skipped=first)
    assert collect_results(junit, returncode=0)[0].status == "not_run"


def test_failure_diagnostics_only_include_fixed_node_status_and_known_type(tmp_path: Path) -> None:
    junit = tmp_path / "results.xml"
    first = next(iter(CASE_TESTS.values()))[0]
    _junit(junit, broken=first)
    document = ET.parse(junit)
    failure = next(document.getroot().iter("failure"))
    failure.text = "AssertionError: synthetic-sensitive-body-should-never-leave-junit"
    failure.set("message", "synthetic-sensitive-credential")
    document.write(junit)
    result = collect_results(junit, returncode=1)[0]
    assert result.tests[0].nodeid == first
    assert result.tests[0].status == "failed"
    assert result.tests[0].error_type == "AssertionError"
    assert "synthetic-sensitive" not in str(result)


def test_runner_executes_real_business_tests_and_missing_postgres_is_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 执行现有业务函数+程序化依赖；强制去掉可连接DB的变量，无后台负载。
    monkeypatch.delenv("OUTBOX_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k never_selected -x")
    result = {item.name: item for item in execute_matrix(ROOT)}
    assert result["vendor_success_response_lost"].status == "passed"
    assert result["vendor_success_mark_submitted_failed"].status == "passed"
    assert result["redis_flush_projection_rebuild"].status == "passed"
    assert result["submitting_timeout_uncertain"].status == "not_run"
    assert result["worker_broker_backlog_drain"].status == "not_run"
