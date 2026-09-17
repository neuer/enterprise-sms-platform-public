"""pytest 门禁分区及实际执行证据；隔离测试不得静默跳过。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from gate_policy import CRITICAL_MARKERS, isolated_node  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    """显式选择完整收集、普通回归或真实数据库回归。"""
    parser.addoption("--gate-shard", choices=("unit", "postgres", "inventory"))
    parser.addoption("--gate-evidence", type=Path)


def pytest_configure(config: pytest.Config) -> None:
    """仅在正式分区命令中注册记录器。"""
    if config.getoption("--gate-shard"):
        config.pluginmanager.register(GateEvidence(config), "sms-gate-evidence")


class GateEvidence:
    """记录完整 nodeid 集合与执行结果，而非只记录测试总数。"""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.shard: str = config.getoption("--gate-shard")
        self.nodes: list[str] = []
        self.passed: set[str] = set()
        self.failed: set[str] = set()
        self.skipped: dict[str, str] = {}
        self.markers: dict[str, list[str]] = {name: [] for name in CRITICAL_MARKERS}
        self.isolated: list[str] = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """分区穷尽全部被收集用例，独立 inventory 用于汇总复核。"""
        selected = []
        deselected = []
        for item in items:
            isolated = isolated_node(item.nodeid)
            if self.shard != "inventory" and isolated != (self.shard == "postgres"):
                deselected.append(item)
                continue
            selected.append(item)
            self.nodes.append(item.nodeid)
            if isolated:
                self.isolated.append(item.nodeid)
            for name in CRITICAL_MARKERS:
                if item.get_closest_marker(name) is not None:
                    self.markers[name].append(item.nodeid)
        items[:] = selected
        if deselected:
            self.config.hook.pytest_deselected(items=deselected)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """setup/call/teardown 的失败均不能被 call passed 覆盖。"""
        if report.failed:
            self.failed.add(report.nodeid)
        if report.skipped:
            self.skipped[report.nodeid] = str(report.longrepr)
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        """写出绑定 SHA 的证据，隔离分区出现任意 skip 即失败关闭。"""
        if self.shard == "postgres" and (self.skipped or not self.nodes):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter = self.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(
                    "gate-evidence: isolated tests must execute without skips", red=True
                )
        path: Path | None = self.config.getoption("--gate-evidence")
        if path is None:
            raise pytest.UsageError("--gate-evidence is required for a gate shard")
        sha = (
            os.environ.get("SMS_GATE_SHA")
            or subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                text=True,
            ).strip()
        )
        document: dict[str, Any] = {
            "schema": 1,
            "sha": sha,
            "shard": self.shard,
            "collected": sorted(self.nodes),
            "passed": sorted(self.passed),
            "failed": sorted(self.failed),
            "skipped": self.skipped,
            "markers": self.markers,
            "isolated": sorted(self.isolated),
            "exit_code": int(session.exitstatus),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
