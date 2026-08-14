from __future__ import annotations

import importlib.util
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "render_security_daily_report.py"
SAMPLE = ROOT / "deploy" / "templates" / "security_daily_report.sample.json"
HTML_PREVIEW = ROOT / "docs" / "previews" / "security-daily-report-sample.html"
TEXT_PREVIEW = ROOT / "docs" / "previews" / "security-daily-report-sample.txt"


def _module() -> ModuleType:
    assert SCRIPT.is_file(), "安全日报渲染器尚未实现"
    spec = importlib.util.spec_from_file_location("render_security_daily_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sample_payload() -> dict[str, Any]:
    assert SAMPLE.is_file(), "安全日报示例数据尚未实现"
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_sample_renders_equivalent_html_and_text() -> None:
    module = _module()
    report = module.load_report(SAMPLE)

    html = module.render_html(report)
    text = module.render_text(report)

    for token in ("服务器安全日报", "待确认", "今日需要你处理", "SSH 与主机安全", "证据范围"):
        assert token in html
        assert token in text
    assert module.render_subject(report) == "[短信平台安全日报][关注] 2026-07-15"


def test_html_escapes_all_report_values() -> None:
    module = _module()
    payload = _sample_payload()
    payload["summary"] = '<script>alert("x")</script>'
    payload["pending_confirmation"] = "确认 A&B"
    payload["audit"][0]["actor"] = "<admin>"

    html = module.render_html(module.parse_report(payload))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "确认 A&amp;B" in html
    assert "&lt;admin&gt;" in html


def test_email_has_no_external_resources_or_sensitive_payloads() -> None:
    module = _module()
    report = module.load_report(SAMPLE)
    html = module.render_html(report)
    text = module.render_text(report)
    rendered = html + text

    assert re.search(r'(?:src|href)=["\']https?://', html, re.I) is None
    assert re.search(r"(?<!\d)1\d{10}(?!\d)", rendered) is None
    assert "Bearer " not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered


def test_email_masks_native_ipv6_but_preserves_audit_payload() -> None:
    module = _module()
    payload = _sample_payload()
    source_ip = "2606:4700:4700:1111:2222:3333:4444:5555"
    payload["audit"][0]["source_ip"] = source_ip

    report = module.parse_report(payload)
    rendered = module.render_html(report) + module.render_text(report)

    assert report.audit[0].source_ip == source_ip
    assert source_ip not in rendered
    assert "2606:4700:4700:1111::/64（IPv6，脱敏）" in rendered


def test_email_keeps_ipv4_audit_source_unchanged() -> None:
    module = _module()
    payload = _sample_payload()
    payload["audit"][0]["source_ip"] = "198.51.100.7"

    report = module.parse_report(payload)
    rendered = module.render_html(report) + module.render_text(report)

    assert "198.51.100.7" in rendered


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.pop("period_end"), "period_end"),
        (lambda payload: payload.update({"unexpected": True}), "unknown"),
        (lambda payload: payload.update({"status": "maybe"}), "status"),
        (
            lambda payload: payload.update({"generated_at": "2026-07-16T08:00:00Z"}),
            "generated_at",
        ),
    ],
)
def test_invalid_top_level_report_fails_closed(
    mutate: Any,
    message: str,
) -> None:
    module = _module()
    payload = _sample_payload()
    mutate(payload)

    with pytest.raises(module.ReportValidationError, match=message):
        module.parse_report(payload)


def test_oversized_or_sensitive_summary_fails_closed() -> None:
    module = _module()
    payload = _sample_payload()
    payload["audit"] = payload["audit"] * 6
    with pytest.raises(module.ReportValidationError, match="audit"):
        module.parse_report(payload)

    payload = _sample_payload()
    payload["summary"] = "发现号码 13800138000"
    with pytest.raises(module.ReportValidationError, match="PII"):
        module.parse_report(payload)

    payload = deepcopy(_sample_payload())
    payload["actions"][0]["token"] = "unsafe"
    with pytest.raises(module.ReportValidationError, match="forbidden"):
        module.parse_report(payload)


def test_checked_in_previews_match_the_sample_renderer() -> None:
    module = _module()
    report = module.load_report(SAMPLE)

    assert HTML_PREVIEW.read_text(encoding="utf-8") == module.render_html(report)
    assert TEXT_PREVIEW.read_text(encoding="utf-8") == module.render_text(report)


def test_html_exposes_mobile_stacking_hooks() -> None:
    module = _module()
    html = module.render_html(module.load_report(SAMPLE))

    for class_name in (
        "header-main",
        "header-date",
        "detail-label",
        "detail-value",
        "detail-assessment",
        "audit-time",
        "audit-actor",
        "audit-action",
        "audit-assessment",
        "coverage-source",
        "coverage-detail",
        "coverage-status",
    ):
        assert f'class="{class_name}"' in html
        assert f".{class_name}" in html


def test_empty_actions_render_as_no_action_needed() -> None:
    module = _module()
    payload = _sample_payload()
    payload["actions"] = []

    html = module.render_html(module.parse_report(payload))
    text = module.render_text(module.parse_report(payload))

    assert "今日需要你处理" in html
    assert "今日无需处理" in html
    assert "今日无需处理" in text
