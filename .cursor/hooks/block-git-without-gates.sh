#!/usr/bin/env bash
# Cursor beforeShellExecution 入口：只输出 hook JSON，实际矩阵在共享脚本。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '{"permission":"deny","user_message":"python3 is required for pre-vcs gates","agent_message":"python3 is required for pre-vcs gates"}'
  exit 0
fi
exec python3 "$ROOT/scripts/check_pre_vcs_gates.py" --cursor-hook
