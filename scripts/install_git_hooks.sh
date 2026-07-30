#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
[[ -d .git ]] || {
  echo "install-hooks: run from the primary Git checkout" >&2
  exit 1
}

chmod +x .githooks/pre-push
git config core.hooksPath .githooks

patterns="$(git rev-parse --git-common-dir)/public-scan-patterns"
if [[ ! -e "$patterns" ]]; then
  umask 077
  printf '%s\n' \
    '# One Python regular expression per line.' \
    '# Add private hostnames, internal identifiers, or personal addresses locally.' \
    >"$patterns"
fi
chmod 600 "$patterns"

echo "install-hooks: core.hooksPath=.githooks private_patterns=$patterns"
