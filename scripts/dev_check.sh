#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
MODE="${1:---changed}"
if [[ $# -gt 0 ]]; then
  shift
fi

usage() {
  cat >&2 <<'EOF'
usage:
  scripts/dev_check.sh --changed [BASE_REF]
  scripts/dev_check.sh --backend [PYTEST_ARG...]
  scripts/dev_check.sh --frontend
  scripts/dev_check.sh --all
EOF
}

case "$MODE" in
  --changed | --backend | --frontend | --all) ;;
  *)
    usage
    exit 2
    ;;
esac

cd "$ROOT"
python3 scripts/check_spec_consistency.py
python3 scripts/check_invariants.py
python3 scripts/check_public_readiness.py

run_contract() {
  bash scripts/local_test.sh prepare
  (
    cd backend
    ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 \
      uv run python ../scripts/check_contract.py ../openapi.yaml
  )
}

run_backend() {
  local -a pytest_args=("$@")
  bash scripts/local_test.sh prepare
  (
    cd backend
    uv run ruff check app migrations scripts_support tests \
      ../scripts/*.py
    uv run mypy app migrations scripts_support \
      ../scripts/check_contract.py \
      ../scripts/check_public_readiness.py \
      ../scripts/classify_ci_changes.py \
      ../scripts/create_release_manifest.py \
      ../scripts/create_offline_image_index.py \
      ../scripts/deploy_release_remote.py \
      ../deploy/scripts/offline_image_archive.py \
      ../deploy/scripts/release_manifest.py \
      ../scripts/verify_ci_results.py
    ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 \
      uv run pytest -q "${pytest_args[@]}"
  )
}

run_frontend() {
  (
    cd frontend
    npm test
    npm run build
  )
}

if [[ "$MODE" == --backend ]]; then
  run_contract
  run_backend "$@"
elif [[ "$MODE" == --frontend ]]; then
  [[ $# -eq 0 ]] || { usage; exit 2; }
  run_frontend
elif [[ "$MODE" == --all ]]; then
  [[ $# -eq 0 ]] || { usage; exit 2; }
  run_contract
  run_backend
  run_frontend
else
  [[ $# -le 1 ]] || { usage; exit 2; }
  BASE_REF="${1:-origin/main}"
  if ! git rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1; then
    echo "dev-check: base ref is unavailable: $BASE_REF" >&2
    exit 2
  fi
  changed=()
  while IFS= read -r -d '' path; do
    changed+=("$path")
  done < <(
    {
      git diff --name-only --no-renames -z "$BASE_REF"...HEAD
      git diff --name-only --no-renames -z
      git diff --cached --name-only --no-renames -z
      git ls-files --others --exclude-standard -z
    } | python3 -c 'import os,sys
seen=set()
for item in sys.stdin.buffer.read().split(b"\0"):
 if item and item not in seen:
  seen.add(item); sys.stdout.buffer.write(item+b"\0")'
  )

  backend_changed=0
  frontend_changed=0
  contract_changed=0
  app_code_changed=0
  backend_tests=()
  shell_scripts=()
  for path in "${changed[@]}"; do
    if [[ "$path" == *.sh && -f "$path" ]]; then
      shell_scripts+=("$path")
    fi
    case "$path" in
      backend/tests/*.py)
        backend_changed=1
        contract_changed=1
        if [[ -f "$path" ]]; then
          backend_tests+=("${path#backend/}")
        fi
        ;;
      backend/app/*)
        backend_changed=1
        app_code_changed=1
        contract_changed=1
        ;;
      backend/* | schema.sql)
        backend_changed=1
        app_code_changed=1
        contract_changed=1
        ;;
      openapi.yaml)
        backend_changed=1
        contract_changed=1
        ;;
      scripts/*.py | scripts/*.sh | deploy/*)
        backend_changed=1
        ;;
      frontend/*)
        frontend_changed=1
        ;;
    esac
  done

  if [[ ${#shell_scripts[@]} -gt 0 ]]; then
    bash -n "${shell_scripts[@]}"
  fi
  if [[ "$contract_changed" == 1 ]]; then
    run_contract
  fi
  if [[ "$backend_changed" == 1 ]]; then
    if [[ "$app_code_changed" == 1 || ${#backend_tests[@]} -eq 0 ]]; then
      run_backend
    else
      run_backend "${backend_tests[@]}"
    fi
  fi
  if [[ "$frontend_changed" == 1 ]]; then
    run_frontend
  fi
  if [[ "$backend_changed" == 0 && "$frontend_changed" == 0 ]]; then
    echo "dev-check: only documentation/metadata changed"
  fi
fi

echo "dev-check: passed mode=$MODE"
