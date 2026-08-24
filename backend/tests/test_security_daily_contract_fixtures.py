"""安全日报脱敏契约防漂移：平台与 mailer 两侧校验对每个夹具的结论必须一致。

平台侧唯一入口是 ``validate_security_daily_payload``（pydantic），mailer 侧是
``render_security_daily_report.parse_report``（stdlib）。两套实现手工对齐，本测试以
共享夹具钉住接受/拒绝结论，防止单侧收紧或放宽后无人察觉。

夹具位于 ``tests/fixtures/security_daily_payload/``：``valid_*.json`` 必须被双方接受，
``invalid_*.json`` 必须被双方拒绝。无效变体均由 ``valid_sample.json``（派生自
``deploy/templates/security_daily_report.sample.json``）做单点变异生成。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.services.security_daily.contract import (
    SecurityDailyValidationError,
    validate_security_daily_payload,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
RENDERER_SCRIPT = SCRIPTS / "render_security_daily_report.py"
FIXTURES = Path(__file__).parent / "fixtures" / "security_daily_payload"


def _renderer() -> ModuleType:
    assert RENDERER_SCRIPT.is_file(), "安全日报渲染器尚未实现"
    scripts_path = str(SCRIPTS)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    spec = importlib.util.spec_from_file_location(
        "render_security_daily_report",
        RENDERER_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _cases() -> list[tuple[Path, bool]]:
    cases = [(path, True) for path in sorted(FIXTURES.glob("valid_*.json"))]
    cases += [(path, False) for path in sorted(FIXTURES.glob("invalid_*.json"))]
    return cases


CASES = _cases()


def _platform_accepts(payload: Any) -> bool:
    try:
        validate_security_daily_payload(payload)
    except SecurityDailyValidationError:
        return False
    return True


def _mailer_accepts(renderer: ModuleType, payload: Any) -> bool:
    try:
        renderer.parse_report(payload)
    except renderer.ReportValidationError:
        return False
    return True


def test_fixture_directory_covers_valid_and_invalid_variants() -> None:
    assert any(expected for _, expected in CASES), "缺少有效夹具"
    assert sum(1 for _, expected in CASES if not expected) >= 5, "无效夹具覆盖不足"


@pytest.mark.parametrize(
    ("path", "expected"),
    CASES,
    ids=[path.name for path, _ in CASES],
)
def test_platform_and_mailer_validation_agree(path: Path, expected: bool) -> None:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    renderer = _renderer()

    platform_ok = _platform_accepts(payload)
    mailer_ok = _mailer_accepts(renderer, payload)

    assert platform_ok == expected, f"平台侧结论漂移: {path.name}"
    assert mailer_ok == expected, f"mailer 侧结论漂移: {path.name}"
