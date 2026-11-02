#!/usr/bin/env bash
# 本地 full G2 与 CI 共用完整分区、执行清单及覆盖率合并入口。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
mode="${1:?unit|postgres|merge|full is required}"
evidence="${2:?an isolated evidence directory is required}"
part_index="${3:-1}"
part_count="${4:-1}"
for number in "$part_index" "$part_count"; do
  [[ "$number" =~ ^([1-9]|1[0-6])$ ]] || { echo 'invalid partition' >&2; exit 2; }
done
shard="$mode"
if [[ "$mode" == unit || "$mode" == postgres ]]; then
  (( part_index <= part_count )) || exit 2
  if (( part_count > 1 )); then
    shard="$mode-$part_index-of-$part_count"
  fi
fi
mkdir -p "$evidence"
evidence="$(cd "$evidence" && pwd -P)"
export UV_LOCKED=1
export ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1
export SMS_GATE_SHA="${SMS_GATE_SHA:-$(git -C "$ROOT" rev-parse HEAD)}"
cd "$ROOT/backend"
export SMS_COVERAGE_CONFIG="$evidence/coverage.ini"
uv run --locked python "$ROOT/scripts/coverage_runtime.py" prepare "$SMS_COVERAGE_CONFIG" \
  --commit "$SMS_GATE_SHA"
export COVERAGE_RCFILE="$SMS_COVERAGE_CONFIG"
case "$mode" in
  unit)
    uv run --locked python "$ROOT/scripts/coverage_runtime.py" begin "$evidence/$shard" \
      --commit "$SMS_GATE_SHA" --shard "$shard"
    COVERAGE_FILE="$evidence/$shard/.coverage" uv run --locked python -m pytest -c pyproject.toml --rootdir=. -q --strict-markers \
      -p scripts_support.gate_evidence --gate-shard unit \
      --gate-part-index "$part_index" --gate-part-count "$part_count" \
      --gate-evidence "$evidence/$shard/tests.json" --junitxml "$evidence/$shard/junit.xml" \
      --durations=30 --cov --cov-config="$SMS_COVERAGE_CONFIG" --cov-report=
    uv run --locked python "$ROOT/scripts/coverage_runtime.py" stamp "$evidence/$shard" \
      --commit "$SMS_GATE_SHA" --shard "$shard"
    ;;
  postgres)
    uv run --locked python "$ROOT/scripts/coverage_runtime.py" begin "$evidence/$shard" \
      --commit "$SMS_GATE_SHA" --shard "$shard"
    COVERAGE_FILE="$evidence/$shard/.coverage" SMS_COVERAGE=1 \
      SMS_TEST_EVIDENCE="$evidence/$shard/tests.json" \
      SMS_TEST_JUNIT="$evidence/$shard/junit.xml" \
      bash "$ROOT/scripts/verify_vendor_postgres_recovery.sh" \
        --gate-part-index "$part_index" --gate-part-count "$part_count"
    uv run --locked python "$ROOT/scripts/coverage_runtime.py" stamp "$evidence/$shard" \
      --commit "$SMS_GATE_SHA" --shard "$shard"
    ;;
  merge)
    coverage_files=()
    for kind in unit postgres; do
      count="$part_index"
      [[ "$kind" != postgres ]] || count="$part_count"
      for index in $(seq 1 "$count"); do
        shard="$kind"
        (( count == 1 )) || shard="$kind-$index-of-$count"
        uv run --locked python "$ROOT/scripts/coverage_runtime.py" verify "$evidence/$shard" \
          --commit "$SMS_GATE_SHA" --shard "$shard"
        coverage_files+=("$evidence/$shard/.coverage")
      done
    done
    uv run --locked python -m pytest -c pyproject.toml --rootdir=. -q --collect-only -p scripts_support.gate_evidence \
      --gate-shard inventory --gate-evidence "$evidence/inventory.json" > "$evidence/collection.txt"
    COVERAGE_FILE="$evidence/.coverage" uv run --locked coverage combine --keep \
      "${coverage_files[@]}"
    COVERAGE_FILE="$evidence/.coverage" uv run --locked coverage json -o "$evidence/coverage.json"
    uv run --locked python "$ROOT/scripts/check_coverage_gates.py" "$evidence/coverage.json" \
      --evidence-dir "$evidence" --commit "$SMS_GATE_SHA" \
      --unit-parts "$part_index" --postgres-parts "$part_count"
    ;;
  full)
    bash "$ROOT/scripts/run_backend_tests.sh" postgres "$evidence"
    bash "$ROOT/scripts/run_backend_tests.sh" unit "$evidence"
    bash "$ROOT/scripts/run_backend_tests.sh" merge "$evidence"
    ;;
  *) echo "invalid backend test mode" >&2; exit 2 ;;
esac
