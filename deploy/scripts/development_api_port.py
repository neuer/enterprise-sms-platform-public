"""开发 API 宿主端口的显式、失败关闭选择；不执行环境文件。"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Mapping
from pathlib import Path


def enabled(environment_file: Path, *, mode: str, environ: Mapping[str, str]) -> bool:
    """仅显式请求且实际容器环境为 development 时允许发布。"""
    requested = environ.get("SMS_DEV_API_PORT", "0")
    if requested == "0":
        return False
    if requested != "1" or mode != "development":
        raise ValueError("development API port requires explicit development mode")
    values: list[str] = []
    for line in environment_file.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assignment = re.fullmatch(
            r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([=:])\s*(.*?)\s*", line
        )
        if assignment is None:
            raise ValueError("development API port requires single-line environment assignments")
        raw_value = assignment[3]
        if raw_value.startswith(('"', "'")) and not re.fullmatch(
            r'''(?:"(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#.*)?''', raw_value
        ):
            raise ValueError("development API port forbids multiline environment values")
        if assignment[1] == "ENVIRONMENT":
            # Compose 也接受冒号和插值；特权选择仅接受单一、无插值的等号字面值。
            # 无空白分隔的 # 属于值，不得误当注释后接受。
            if assignment[2] != "=" or not re.fullmatch(
                r'''(?:development|"development"|'development')(?:\s+#.*)?''', raw_value
            ):
                raise ValueError("development API port requires literal development environment")
            values.append("development")
    if values != ["development"]:
        raise ValueError("development API port requires one ENVIRONMENT=development")
    if environ.get("ENVIRONMENT", "development") != "development":
        raise ValueError("development API port conflicts with shell environment")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--mode", required=True)
    arguments = parser.parse_args()
    try:
        if not enabled(arguments.environment_file, mode=arguments.mode, environ=os.environ):
            raise ValueError("development API port was not explicitly requested")
    except (OSError, UnicodeError, ValueError):
        parser.exit(1, "development API port configuration rejected\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
