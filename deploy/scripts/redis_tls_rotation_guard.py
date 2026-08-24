#!/usr/bin/env python3
"""阻断普通 backend 轮换夹带 Redis TLS 三域 tuple 变化。"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from prepare_runtime_secrets import (
    RuntimeSecretsError,
    verify_ordinary_redis_tls_rotation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an ordinary Redis rotation")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--baseline-target", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只输出通过/失败状态，不输出证书、私钥或其指纹。"""

    arguments = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print("Redis TLS rotation guard requires root", file=sys.stderr)
        return 1
    try:
        verify_ordinary_redis_tls_rotation(
            source_dir=arguments.source_dir,
            runtime_root=arguments.runtime_root,
            baseline_target=arguments.baseline_target,
        )
    except RuntimeSecretsError as exc:
        print(f"Redis TLS rotation guard failed: {exc}", file=sys.stderr)
        return 1
    print("Redis TLS rotation tuple unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
