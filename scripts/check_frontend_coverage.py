"""前端全量源码分母与 API/会话分组覆盖门禁；不信任报告中的百分比。"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

# 基于 a5be9ffd 的 129 文件实测基线；降低门槛必须作为独立政策变更审查。
THRESHOLDS: Mapping[str, tuple[int, int]] = MappingProxyType({
    "frontend": (85, 82),
    "api": (90, 80),
    "session": (90, 82),
    "composables": (93, 87),
})
SESSION_FILES = frozenset({
    "src/api/auth.ts", "src/api/client.ts", "src/api/httpDeadline.ts",
    "src/api/refreshLock.ts", "src/api/adminStepUp.ts", "src/stores/session.ts",
    "src/router/index.ts", "src/composables/useAdminStepUp.ts",
})
CRITICAL_TEST_FILES = frozenset({
    "tests/api-request.test.ts", "tests/auth-api.test.ts", "tests/session.test.ts",
    "tests/refresh-lock.test.ts", "tests/router-auth.test.ts", "tests/admin-step-up.test.ts",
    "tests/session-generation.test.ts", "tests/session-retire-generation.test.ts",
    "tests/client-refresh-cancel.test.ts", "tests/client-terminal-session.test.ts",
})


class FrontendCoverageError(ValueError):
    """证据缺失、分母改变、测试失败或覆盖不足。"""


def source_inventory(root: Path) -> set[str]:
    """独立枚举所有生产 TS/Vue；只排除生成类型和无运行语句的声明文件。"""
    return {
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*")
        if path.is_file() and path.suffix in {".ts", ".vue"}
        and not path.name.endswith(".d.ts")
        and path.relative_to(root).as_posix() != "src/api/types.gen.ts"
    }


def _relative(value: object, root: Path) -> str:
    if not isinstance(value, str) or not value:
        raise FrontendCoverageError("file path is invalid")
    path = Path(value)
    try:
        return (path if path.is_absolute() else root / path).resolve().relative_to(
            root.resolve()
        ).as_posix()
    except ValueError:
        raise FrontendCoverageError("file path is outside frontend") from None


def _counts(entry: Any, metric: str) -> tuple[int, int]:
    if not isinstance(entry, dict) or not isinstance(entry.get(metric), dict):
        raise FrontendCoverageError("coverage counters are missing")
    item = entry[metric]
    total, covered = item.get("total"), item.get("covered")
    if type(total) is not int or type(covered) is not int or not 0 <= covered <= total:
        raise FrontendCoverageError("coverage counters are invalid")
    if type(item.get("skipped", 0)) is not int or item.get("skipped", 0) != 0:
        raise FrontendCoverageError("skipped coverage counters are forbidden")
    return total, covered


def _groups(path: str) -> set[str]:
    groups = {"frontend"}
    if path.startswith("src/api/"):
        groups.add("api")
    if path.startswith("src/api/session") or path in SESSION_FILES:
        groups.add("session")
    if path.startswith("src/composables/"):
        groups.add("composables")
    return groups


def evaluate_coverage(document: Any, *, root: Path) -> dict[str, dict[str, float]]:
    """按整数分子/分母汇总；完整文件清单与独立分组门槛同时满足才能通过。"""
    if not isinstance(document, dict) or "total" not in document:
        raise FrontendCoverageError("coverage summary is missing")
    files: dict[str, Any] = {}
    for key, value in document.items():
        if key == "total":
            continue
        path = _relative(key, root)
        if path in files:
            raise FrontendCoverageError("duplicate normalized coverage file")
        files[path] = value
    expected = source_inventory(root)
    if not expected or set(files) != expected:
        raise FrontendCoverageError("complete source inventory mismatch")
    totals: dict[str, dict[str, float]] = {}
    for group, floors in THRESHOLDS.items():
        selected = [value for path, value in files.items() if group in _groups(path)]
        totals[group] = {}
        for metric, floor in zip(("statements", "branches"), floors, strict=True):
            counts = [_counts(value, metric) for value in selected]
            total = sum(item[0] for item in counts)
            covered = sum(item[1] for item in counts)
            if total == 0:
                raise FrontendCoverageError(f"{group} {metric} evidence is missing")
            if group == "frontend" and _counts(document["total"], metric) != (total, covered):
                raise FrontendCoverageError("aggregate coverage counters mismatch")
            totals[group][metric] = covered * 100 / total
            if covered * 100 < total * floor:
                raise FrontendCoverageError(f"{group} {metric} below required {floor}%")
    return totals


def verify_tests(document: Any, *, root: Path) -> None:
    """要求完整测试文件集、关键安全测试以及每个测试断言实际成功。"""
    if not isinstance(document, dict) or document.get("success") is not True:
        raise FrontendCoverageError("test execution did not succeed")
    expected = {
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("*")
        if path.name.endswith((".test.ts", ".spec.ts"))
    }
    if not expected >= CRITICAL_TEST_FILES:
        raise FrontendCoverageError("required security test file is missing")
    results = document.get("testResults")
    if not isinstance(results, list):
        raise FrontendCoverageError("test results are missing")
    observed: set[str] = set()
    assertions = 0
    for result in results:
        if not isinstance(result, dict) or result.get("status") != "passed":
            raise FrontendCoverageError("test file failed or was skipped")
        path = _relative(result.get("name"), root)
        if path in observed:
            raise FrontendCoverageError("duplicate test file")
        observed.add(path)
        tests = result.get("assertionResults")
        if not isinstance(tests, list) or not tests or any(
            not isinstance(item, dict) or item.get("status") != "passed" for item in tests
        ):
            raise FrontendCoverageError("test assertion failed, missing or skipped")
        assertions += len(tests)
    if not expected or observed != expected:
        raise FrontendCoverageError("complete test inventory mismatch")
    if type(document.get("numTotalTests")) is not int or document["numTotalTests"] != assertions:
        raise FrontendCoverageError("test count mismatch")


def input_hash(root: Path) -> str:
    """绑定生产源码、测试与构建/覆盖配置，禁止复用改动前的通过报告。"""
    import hashlib

    names = source_inventory(root) | {
        path.relative_to(root).as_posix()
        for folder in ("tests", "scripts")
        for path in (root / folder).rglob("*") if path.is_file()
    } | {"package.json", "package-lock.json", "vite.config.ts", "tsconfig.json"}
    digest = hashlib.sha256()
    for name in sorted(names):
        digest.update(name.encode() + b"\0")
        digest.update((root / name).read_bytes() + b"\0")
    digest.update(Path(__file__).read_bytes())
    return digest.hexdigest()


def verify_evidence(directory: Path, *, root: Path, commit: str) -> dict[str, dict[str, float]]:
    """独立消费同 SHA 的成功分片与完整覆盖报告；缺失或损坏即失败。"""
    import hashlib
    import json
    import re

    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise FrontendCoverageError("expected commit is invalid")
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise FrontendCoverageError("invalid evidence manifest")
        if manifest.get("commit") != commit or manifest.get("inputs") != input_hash(root):
            raise FrontendCoverageError("commit or source/configuration mismatch")
        if manifest.get("shard") != "frontend" or manifest.get("success") is not True:
            raise FrontendCoverageError("required frontend shard failed or is missing")
        documents = {}
        for name in ("coverage-summary.json", "tests.json"):
            raw = (directory / name).read_bytes()
            if not isinstance(manifest.get("reports"), dict) or (
                manifest["reports"].get(name) != hashlib.sha256(raw).hexdigest()
            ):
                raise FrontendCoverageError("report hash mismatch")
            documents[name] = json.loads(raw)
        verify_tests(documents["tests.json"], root=root)
        return evaluate_coverage(documents["coverage-summary.json"], root=root)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrontendCoverageError("coverage evidence is missing or corrupt") from exc


def run_coverage(directory: Path, *, root: Path, commit: str) -> dict[str, dict[str, float]]:
    """用全新临时目录执行完整 Vitest 后封存证据，不消费上次遗留报告。"""
    import hashlib
    import json
    import shutil
    import subprocess
    import tempfile

    before = input_hash(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps({
        "schema": 1, "commit": commit, "inputs": before,
        "shard": "frontend", "success": False,
    }) + "\n")
    subprocess.run(
        ["node", str(root / "scripts/verify-coverage-denominator.mjs")], cwd=root, check=True,
    )
    with tempfile.TemporaryDirectory(prefix="sms-frontend-coverage-") as temporary:
        stage = Path(temporary)
        subprocess.run(
            ["npm", "exec", "--", "vitest", "run", "--coverage", "--reporter=default",
             "--reporter=json", f"--outputFile.json={stage / 'tests.json'}",
             f"--coverage.reportsDirectory={stage / 'report'}"],
            cwd=root, check=True,
        )
        if input_hash(root) != before:
            raise FrontendCoverageError("inputs changed during test execution")
        shutil.copyfile(stage / "report/coverage-summary.json", stage / "coverage-summary.json")
        manifest = {"schema": 1, "commit": commit, "inputs": before,
                    "shard": "frontend", "success": True,
                    "reports": {name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
                                for name in ("coverage-summary.json", "tests.json")}}
        (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        totals = verify_evidence(stage, root=root, commit=commit)
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("coverage-summary.json", "tests.json", "manifest.json"):
            shutil.copyfile(stage / name, directory / name)
        shutil.copyfile(stage / "report/lcov.info", directory / "lcov.info")
        return totals


def main() -> int:
    """供 npm 和 CI 共用的收集/验收入口。"""
    import argparse
    import json
    import os
    import subprocess
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "verify"))
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / "frontend",
    )
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--commit")
    args = parser.parse_args()
    root = args.root.resolve()
    directory = (args.evidence_dir or root / "coverage").resolve()
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        commit = args.commit or os.environ.get("GITHUB_SHA") or head
        if commit != head:
            raise FrontendCoverageError("expected commit does not match checkout")
        action = run_coverage if args.mode == "run" else verify_evidence
        print(json.dumps(action(directory, root=root, commit=commit), sort_keys=True))
    except (FrontendCoverageError, OSError, subprocess.CalledProcessError) as exc:
        print(f"frontend coverage gate failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
