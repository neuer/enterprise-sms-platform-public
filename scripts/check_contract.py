#!/usr/bin/env python3
"""比较 FastAPI 实际 OpenAPI 与根目录契约的可执行表面。"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
DOC_ONLY_KEYS = {
    "description",
    "summary",
    "title",
    "example",
    "examples",
    "operationId",
    "tags",
    "externalDocs",
}


def load_symbol(value: str) -> Any:
    working_directory = str(Path.cwd())
    if working_directory not in sys.path:
        sys.path.insert(0, working_directory)
    module_name, symbol_name = value.split(":", 1)
    return getattr(importlib.import_module(module_name), symbol_name)


def resolve_ref(value: Any, document: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {"$ref"}:
        ref = value["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"只允许本地 $ref: {ref}")
        target: Any = document
        for token in ref[2:].split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
        return resolve_ref(deepcopy(target), document)
    if isinstance(value, dict):
        return {key: resolve_ref(item, document) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_ref(item, document) for item in value]
    return value


def normalize(value: Any, document: dict[str, Any]) -> Any:
    value = resolve_ref(value, document)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in DOC_ONLY_KEYS:
                continue
            result[key] = normalize(item, document)
        for ordered_key in ("required", "enum"):
            if ordered_key in result and isinstance(result[ordered_key], list):
                result[ordered_key] = sorted(
                    result[ordered_key],
                    key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
                )
        return result
    if isinstance(value, list):
        return [normalize(item, document) for item in value]
    return value


def operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path, path_item in document.get("paths", {}).items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS:
                result[(path, method.lower())] = operation
    return result


def drop_fastapi_default_422(operation: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    operation = deepcopy(operation)
    if "422" not in expected.get("responses", {}):
        response = operation.get("responses", {}).get("422")
        if response and "HTTPValidationError" in json.dumps(response, ensure_ascii=False):
            operation["responses"].pop("422", None)
    return operation


def compare_documents(
    expected_doc: dict[str, Any],
    actual_doc: dict[str, Any],
    *,
    allow_missing: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """比较契约；阶段门禁可暂容未来路由缺失，但绝不容忍额外或漂移。"""

    expected_ops = operations(expected_doc)
    actual_ops = operations(actual_doc)
    missing = [] if allow_missing else sorted(set(expected_ops) - set(actual_ops))
    extra = sorted(set(actual_ops) - set(expected_ops))
    diffs: list[str] = []

    for key in sorted(set(expected_ops) & set(actual_ops)):
        expected = normalize(expected_ops[key], expected_doc)
        actual_raw = drop_fastapi_default_422(actual_ops[key], expected_ops[key])
        actual = normalize(actual_raw, actual_doc)
        if expected != actual:
            diffs.append(
                f"{key[1].upper()} {key[0]}\n"
                f"EXPECTED={json.dumps(expected, ensure_ascii=False, sort_keys=True)}\n"
                f"ACTUAL={json.dumps(actual, ensure_ascii=False, sort_keys=True)}"
            )
    return missing, extra, diffs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", type=Path)
    parser.add_argument("--app", default="app.main:app")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="阶段门禁：允许尚未实现的未来路由缺失，已实现路由仍严格比较",
    )
    args = parser.parse_args()

    expected_doc = yaml.safe_load(args.spec.read_text(encoding="utf-8"))
    app = load_symbol(args.app)
    actual_doc = app.openapi()

    missing, extra, diffs = compare_documents(
        expected_doc,
        actual_doc,
        allow_missing=args.allow_missing,
    )

    if missing or extra or diffs:
        if missing:
            print(
                "缺少实现路由:",
                *[f"{method.upper()} {path}" for path, method in missing],
                sep="\n  ",
                file=sys.stderr,
            )
        if extra:
            print(
                "契约外实现路由:",
                *[f"{method.upper()} {path}" for path, method in extra],
                sep="\n  ",
                file=sys.stderr,
            )
        for diff in diffs:
            print(f"契约字段差异:\n{diff}", file=sys.stderr)
        return 1

    mode = "阶段子集" if args.allow_missing else "完整"
    print(f"契约一致性检查通过: {len(operations(actual_doc))} 个已实现 operation（{mode}模式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
