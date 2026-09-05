#!/usr/bin/env bash
# Optional Cursor backup: deny git commit/push that skip hooks.
# The main gate is .githooks/pre-commit and pre-push after
# scripts/install_git_hooks.sh. This is not the human enable step.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '{"permission":"deny","user_message":"python3 is required for pre-vcs gates","agent_message":"python3 is required for pre-vcs gates"}'
  exit 0
fi
exec python3 "$ROOT/scripts/check_pre_vcs_gates.py" --cursor-hook
