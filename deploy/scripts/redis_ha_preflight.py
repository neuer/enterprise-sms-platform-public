#!/usr/bin/env python3
"""生产 Redis 拓扑预检：只有 managed 可称为 HA；standalone 不得伪装。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ALLOWED_PRODUCTION = frozenset({"managed", "isolated-standalone"})
HA_CLAIM_FORBIDDEN = (
    "high availability",
    "高可用 Redis",
    "sentinel ha",
    "cluster ha",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument(
        "--docs",
        default=str(Path(__file__).resolve().parents[1] / "redis-ha.md"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = args.mode.strip()
    if mode == "standalone":
        print("sms-compose: standalone must not be labeled production HA", file=sys.stderr)
        return 1
    if mode not in ALLOWED_PRODUCTION:
        print("sms-compose: production REDIS_HA_MODE is invalid", file=sys.stderr)
        return 1
    docs = Path(args.docs)
    if not docs.is_file():
        print("sms-compose: redis-ha.md is missing", file=sys.stderr)
        return 1
    text = docs.read_text(encoding="utf-8")
    if "不得把此单机形态写成 `managed`" not in text:
        print("sms-compose: redis-ha.md lost standalone-is-not-HA contract", file=sys.stderr)
        return 1
    if mode == "isolated-standalone":
        if "不得描述为高可用" not in text:
            print("sms-compose: isolated-standalone must not be documented as HA", file=sys.stderr)
            return 1
        lowered = text.lower()
        if "phase 0 选择 `isolated-standalone`" in text and any(
            phrase in lowered for phrase in ("this is ha", "phase 0 is ha")
        ):
            print("sms-compose: isolated-standalone is labeled HA", file=sys.stderr)
            return 1
    if mode == "managed" and "`managed` 只用于三个独立托管高可用端点" not in text:
        print("sms-compose: managed HA contract missing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
