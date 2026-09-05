#!/usr/bin/env bash
# Enable this repo's git pre-commit / pre-push gates.
# This is the human enable step — not Cursor Settings → Hooks.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "install-hooks: run from a Git work tree" >&2
  exit 1
fi

chmod +x .githooks/pre-commit .githooks/pre-push
git config --local core.hooksPath .githooks

patterns="$(git rev-parse --git-common-dir)/public-scan-patterns"
if [[ ! -e "$patterns" ]]; then
  umask 077
  printf '%s\n' \
    '# One Python regular expression per line.' \
    '# Add private hostnames, internal identifiers, or personal addresses locally.' \
    >"$patterns"
fi
chmod 600 "$patterns"

echo "install-hooks: enabled local git pre-commit/pre-push (core.hooksPath=.githooks)"
echo "install-hooks: this is the enable step; Cursor Settings is not required"
echo "install-hooks: private_patterns=$patterns"
