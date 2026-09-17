#!/usr/bin/env bash
# 本地 full G2 与 CI 共用完整分区、执行清单及覆盖率合并入口。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
mode="${1:?unit|postgres|merge|full is required}"
evidence="${2:?an isolated evidence directory is required}"
mkdir -p "$evidence"
evidence="$(cd "$evidence" && pwd -P)"
export UV_LOCKED=1
export ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1
export SMS_GATE_SHA="${SMS_GATE_SHA:-$(git -C "$ROOT" rev-parse HEAD)}"
cd "$ROOT/backend"
case "$mode" in
  unit)
    mkdir -p "$evidence/unit"
    COVERAGE_FILE="$evidence/unit/.coverage" uv run --locked python -m pytest -c pyproject.toml --rootdir=. -q --strict-markers \
      -p scripts_support.gate_evidence --gate-shard unit \
      --gate-evidence "$evidence/unit/tests.json" --junitxml "$evidence/unit/junit.xml" \
      --durations=30 --cov=app --cov-report=
    ;;
  postgres)
    mkdir -p "$evidence/postgres"
    COVERAGE_FILE="$evidence/postgres/.coverage" SMS_COVERAGE=1 \
      SMS_TEST_EVIDENCE="$evidence/postgres/tests.json" \
      SMS_TEST_JUNIT="$evidence/postgres/junit.xml" \
      bash "$ROOT/scripts/verify_vendor_postgres_recovery.sh"
    ;;
  merge)
    uv run --locked python -m pytest -c pyproject.toml --rootdir=. -q --collect-only -p scripts_support.gate_evidence \
      --gate-shard inventory --gate-evidence "$evidence/inventory.json" > "$evidence/collection.txt"
    COVERAGE_FILE="$evidence/.coverage" uv run --locked coverage combine --keep \
      "$evidence/unit/.coverage" "$evidence/postgres/.coverage"
    COVERAGE_FILE="$evidence/.coverage" uv run --locked coverage json -o "$evidence/coverage.json"
    uv run --locked python "$ROOT/scripts/check_coverage_gates.py" "$evidence/coverage.json" \
      --evidence-dir "$evidence" --commit "$SMS_GATE_SHA"
    ;;
  full)
    bash "$ROOT/scripts/run_backend_tests.sh" postgres "$evidence"
    bash "$ROOT/scripts/run_backend_tests.sh" unit "$evidence"
    bash "$ROOT/scripts/run_backend_tests.sh" merge "$evidence"
    ;;
  *) echo "invalid backend test mode" >&2; exit 2 ;;
esac
