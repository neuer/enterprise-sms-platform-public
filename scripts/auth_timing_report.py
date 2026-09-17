#!/usr/bin/env python3
"""离线汇总批准目录的脱敏交错样本；不连接 LDAP，不读取或输出账号/凭据。"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

CASES = {"missing", "wrong_password", "correct", "disabled", "locked", "unavailable"}
MODES = {"cold_single", "warm_single", "cold_multi", "warm_multi"}


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    at = (len(ordered) - 1) * quantile
    lower = int(at)
    return ordered[lower] + (
        ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]
    ) * (at - lower)


def rank_auc(first: list[float], second: list[float]) -> float:
    """用有并列修正的秩统计计算方向无关的单变量时间分类 AUC。"""

    pooled = sorted([(value, 0) for value in first] + [(value, 1) for value in second])
    rank_sum = 0.0
    index = 0
    while index < len(pooled):
        end = index + 1
        while end < len(pooled) and pooled[end][0] == pooled[index][0]:
            end += 1
        rank_sum += sum(label for _, label in pooled[index:end]) * (index + 1 + end) / 2
        index = end
    n = len(second)
    auc = (rank_sum - n * (n + 1) / 2) / (len(first) * n)
    return max(auc, 1 - auc)


def report(payload: Any) -> dict[str, Any]:
    """只接受固定非身份列；统计输出不代表目录安全或功效已经验收。"""

    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "samples"}
        or payload["version"] != 1
    ):
        raise ValueError("invalid sample envelope")
    samples = payload["samples"]
    if not isinstance(samples, list) or not 2 <= len(samples) <= 20000:
        raise ValueError("invalid sample count")
    groups: dict[tuple[str, str], list[float]] = {}
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {
            "case",
            "mode",
            "duration_ms",
        }:
            raise ValueError("only redacted sample fields are accepted")
        case, mode, value = sample["case"], sample["mode"], sample["duration_ms"]
        if (
            case not in CASES
            or mode not in MODES
            or type(value) not in {int, float}
            or not math.isfinite(value)
            or not 0 <= value <= 120000
        ):
            raise ValueError("invalid sample value")
        groups.setdefault((mode, case), []).append(float(value))
    result: dict[str, Any] = {
        "evidence_kind": "offline_statistics",
        "acceptance": "not_evaluated",
        "groups": [],
        "comparisons": [],
    }
    for (mode, case), values in sorted(groups.items()):
        result["groups"].append(
            {
                "mode": mode,
                "case": case,
                "count": len(values),
                "p50_ms": percentile(values, 0.5),
                "p90_ms": percentile(values, 0.9),
                "p95_ms": percentile(values, 0.95),
                "p99_ms": percentile(values, 0.99),
                "stddev_ms": statistics.pstdev(values),
            }
        )
    rng = random.Random(0)  # 统计重采样可复现；不参与任何安全随机值生成。
    for mode in sorted(MODES):
        first, second = (
            groups.get((mode, "missing"), []),
            groups.get((mode, "wrong_password"), []),
        )
        if not first or not second:
            continue
        if len(first) > 5000 or len(second) > 5000:
            raise ValueError("comparison sample budget exceeded")
        estimates = [
            rank_auc(
                rng.choices(first, k=len(first)), rng.choices(second, k=len(second))
            )
            for _ in range(200)
        ]
        result["comparisons"].append(
            {
                "mode": mode,
                "auc": rank_auc(first, second),
                "auc_bootstrap_95pct": [
                    percentile(estimates, 0.025),
                    percentile(estimates, 0.975),
                ],
                "median_gap_ms": abs(percentile(first, 0.5) - percentile(second, 0.5)),
                "p95_gap_ms": abs(percentile(first, 0.95) - percentile(second, 0.95)),
                "complete_case_coverage": all((mode, case) in groups for case in CASES),
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("sample file too large")
        result = report(json.loads(raw))
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (ValueError, OSError, TypeError):
        print("样本无效；仅允许脱敏类别、模式和耗时，不回显输入。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
