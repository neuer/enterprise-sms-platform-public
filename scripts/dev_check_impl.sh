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
python3 scripts/check_pre_vcs_gates.py --require-hooks-path
python3 scripts/check_spec_consistency.py
python3 scripts/check_invariants.py
python3 scripts/check_public_readiness.py

run_contract() {
  bash scripts/local_test.sh prepare
  (
    cd backend
    ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 \
      uv run --locked python ../scripts/check_contract.py ../openapi.yaml
  )
}

run_backend() {
  local -a pytest_args=("$@")
  bash scripts/local_test.sh prepare
  (
    cd backend
    python3 ../scripts/check_backend_static.py
    ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 \
      uv run --locked python -m pytest -q "${pytest_args[@]}"
  )
}

run_frontend() {
  (cd backend; ENVIRONMENT=test DEBUG=1 AUTH_MOCK=1 VENDOR_MOCK=1 \
    uv run --locked python -m pytest -q tests/test_frontend_contract.py)
  (
    cd frontend
    # lint/format 快于组件测试，失败早报错
    npm run lint
    npm run format:check
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
  # 保存已核验的命令结果后再读取；不能在进程替换中吞掉 Git 失败。
  changed_file="$(mktemp)"
  trap 'rm -f "$changed_file"' EXIT
  git diff --name-only --no-renames -z "$BASE_REF"...HEAD > "$changed_file"
  changed=()
  while IFS= read -r -d '' path; do changed+=("$path"); done < "$changed_file"

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
      backend/tests/conftest.py | backend/tests/helpers/* | backend/tests/integration/*)
        backend_changed=1
        app_code_changed=1
        contract_changed=1
        ;;
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
      scripts/*.py | scripts/*.sh | deploy/* | .github/* | .githooks/*)
        backend_changed=1
        app_code_changed=1
        ;;
      frontend/*)
        frontend_changed=1
        ;;
    esac
  done

  if [[ ${#shell_scripts[@]} -gt 0 ]]; then
    for script in "${shell_scripts[@]}"; do bash -n "$script"; done
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
