"""短信模板占位校验、平台渲染与厂商格式转换的唯一实现。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PLATFORM_PLACEHOLDER = re.compile(r"\{([1-9]\d*)\}")
BRACED_TOKEN = re.compile(r"\{[^{}]*\}")
MAX_RENDERED_LENGTH = 500
MAX_VARIABLE_LENGTH = 100


class TemplateParamMismatch(ValueError):
    """模板定义或调用参数不匹配，对应平台 422 错误。"""

    code = "TEMPLATE_PARAM_MISMATCH"
    status_code = 422

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(frozen=True, slots=True)
class VarSpec:
    """单个模板变量的位置和最大允许字符数。"""

    pos: int
    max_len: int


VarSpecInput = VarSpec | Mapping[str, object]


def _mismatch(message: str, **detail: Any) -> TemplateParamMismatch:
    return TemplateParamMismatch(message, detail=detail or None)


def _coerce_spec(raw: VarSpecInput) -> VarSpec:
    if isinstance(raw, VarSpec):
        spec = raw
    elif isinstance(raw, Mapping):
        pos = raw.get("pos")
        max_len = raw.get("max_len")
        if (
            not isinstance(pos, int)
            or isinstance(pos, bool)
            or not isinstance(max_len, int)
            or isinstance(max_len, bool)
        ):
            raise _mismatch("var_specs 的 pos 与 max_len 必须为整数")
        spec = VarSpec(pos=pos, max_len=max_len)
    else:
        raise _mismatch("var_specs 格式无效")

    if spec.pos < 1:
        raise _mismatch("var_specs.pos 必须从 1 开始", pos=spec.pos)
    if not 1 <= spec.max_len <= MAX_VARIABLE_LENGTH:
        raise _mismatch(
            f"变量 {spec.pos} 的 max_len 必须在 1 到 {MAX_VARIABLE_LENGTH} 之间",
            pos=spec.pos,
            max_len=spec.max_len,
        )
    return spec


def _validated_specs(content: str, var_specs: Sequence[VarSpecInput]) -> tuple[VarSpec, ...]:
    """校验占位和声明严格形成连续的一一映射。"""

    if not isinstance(content, str):
        raise _mismatch("模板内容必须为字符串")

    specs = tuple(sorted((_coerce_spec(raw) for raw in var_specs), key=lambda item: item.pos))
    positions = tuple(spec.pos for spec in specs)
    expected = tuple(range(1, len(specs) + 1))
    if positions != expected:
        raise _mismatch("var_specs 位置必须唯一且从 1 连续递增")

    tokens = BRACED_TOKEN.findall(content)
    placeholders = PLATFORM_PLACEHOLDER.findall(content)
    if len(tokens) != len(placeholders):
        raise _mismatch("模板仅允许使用 {1}..{n} 格式占位")

    used_positions = {int(position) for position in placeholders}
    if used_positions != set(expected):
        raise _mismatch(
            "模板占位与 var_specs 必须一一对应",
            placeholders=sorted(used_positions),
            declared=list(expected),
        )
    return specs


def render_template(
    content: str,
    var_specs: Sequence[VarSpecInput],
    params: Sequence[str],
) -> str:
    """校验参数并全量替换平台占位，结果最长 500 字符。"""

    specs = _validated_specs(content, var_specs)
    if len(params) != len(specs):
        raise _mismatch(
            "模板参数个数不符",
            expected=len(specs),
            actual=len(params),
        )

    values: dict[int, str] = {}
    for spec, value in zip(specs, params, strict=True):
        if not isinstance(value, str):
            raise _mismatch("模板参数必须为字符串", pos=spec.pos)
        actual_len = len(value)
        if actual_len > spec.max_len:
            raise _mismatch(
                f"模板参数 {spec.pos} 长度超过 max_len",
                pos=spec.pos,
                max_len=spec.max_len,
                actual_len=actual_len,
            )
        values[spec.pos] = value

    rendered = PLATFORM_PLACEHOLDER.sub(
        lambda match: values[int(match.group(1))],
        content,
    )
    if len(rendered) > MAX_RENDERED_LENGTH:
        raise _mismatch(
            f"模板渲染结果长度不能超过 {MAX_RENDERED_LENGTH}",
            max_len=MAX_RENDERED_LENGTH,
            actual_len=len(rendered),
        )
    return rendered


def to_vendor_template(content: str, var_specs: Sequence[VarSpecInput]) -> str:
    """把平台 `{n}` 占位按位置转换成厂商 `{s<max_len>}` 格式。"""

    specs = _validated_specs(content, var_specs)
    max_lengths = {spec.pos: spec.max_len for spec in specs}
    return PLATFORM_PLACEHOLDER.sub(
        lambda match: f"{{s{max_lengths[int(match.group(1))]}}}",
        content,
    )
