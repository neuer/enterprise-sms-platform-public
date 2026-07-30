from __future__ import annotations

import pytest

from app.services.template import (
    TemplateParamMismatch,
    VarSpec,
    render_template,
    to_vendor_template,
)

SPECS = (VarSpec(pos=1, max_len=10), VarSpec(pos=2, max_len=6))


def test_render_replaces_all_occurrences_in_placeholder_order() -> None:
    rendered = render_template(
        "尊敬的{1}，验证码{2}，再次确认{2}",
        SPECS,
        ["张三", "123456"],
    )

    assert rendered == "尊敬的张三，验证码123456，再次确认123456"


def test_vendor_conversion_uses_each_declared_max_length() -> None:
    assert (
        to_vendor_template("尊敬的{1}，验证码{2}", SPECS)
        == "尊敬的{s10}，验证码{s6}"
    )


@pytest.mark.parametrize("params", [[], ["张三"], ["张三", "123456", "多余"]])
def test_render_rejects_parameter_count_mismatch(params: list[str]) -> None:
    with pytest.raises(TemplateParamMismatch) as captured:
        render_template("尊敬的{1}，验证码{2}", SPECS, params)

    assert captured.value.code == "TEMPLATE_PARAM_MISMATCH"
    assert captured.value.status_code == 422


def test_render_rejects_parameter_longer_than_var_spec() -> None:
    with pytest.raises(TemplateParamMismatch) as captured:
        render_template("验证码{1}", [{"pos": 1, "max_len": 6}], ["1234567"])

    assert captured.value.detail == {"pos": 1, "max_len": 6, "actual_len": 7}


def test_render_rejects_result_over_500_characters() -> None:
    with pytest.raises(TemplateParamMismatch, match="500"):
        render_template("甲" * 499 + "{1}", [VarSpec(1, 2)], ["乙丙"])


@pytest.mark.parametrize(
    ("content", "specs"),
    [
        ("跳号{2}", [VarSpec(2, 5)]),
        ("缺声明{1}{2}", [VarSpec(1, 5)]),
        ("重复声明{1}", [VarSpec(1, 5), VarSpec(1, 6)]),
        ("无效长度{1}", [VarSpec(1, 0)]),
        ("声明但无占位", [VarSpec(1, 5)]),
    ],
)
def test_invalid_template_definition_is_rejected(
    content: str,
    specs: list[VarSpec],
) -> None:
    with pytest.raises(TemplateParamMismatch):
        to_vendor_template(content, specs)


def test_plain_template_accepts_no_parameters() -> None:
    assert render_template("系统维护通知", [], []) == "系统维护通知"
    assert to_vendor_template("系统维护通知", []) == "系统维护通知"
