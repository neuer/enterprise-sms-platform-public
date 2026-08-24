#!/usr/bin/env python3
"""Fail closed unless the production host-tools Python contract is available."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def main() -> int:
    if sys.argv[1:] != ["lifecycle"]:
        print("host Python preflight received an invalid profile", file=sys.stderr)
        return 2
    if sys.executable != "/usr/bin/python3" or sys.version_info[:2] != (3, 12):
        print("host Python preflight requires /usr/bin/python3 version 3.12", file=sys.stderr)
        return 1
    scripts = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts))
    try:
        for module in (
            "failover_common",
            "sync_standby",
            "restore_drill",
            "lifecycle_manager",
        ):
            importlib.import_module(module)
    except Exception:
        print("host Python lifecycle imports are unavailable", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "event": "host_python_preflight",
                "profile": "lifecycle",
                "status": "passed",
                "version": "3.12",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
