#!/usr/bin/env bash
# scripts/verify_all.sh — 受保护变更、专项复验与生产候选的完整 G2 门禁
# 约定：项目根执行；ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1；禁止削弱本脚本（只能修被测对象）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
cd "$ROOT"
mode="full"
include_performance=0
include_release_control=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      [[ $# -ge 2 ]] || exit 2
      mode="$2"
      shift 2
      ;;
    --include-performance)
      include_performance=1
      shift
      ;;
    --include-release-control)
      include_release_control=1
      shift
      ;;
    *)
      echo "usage: scripts/verify_all.sh [--mode full|integration] [--include-performance] [--include-release-control]" >&2
      exit 2
      ;;
  esac
done
case "$mode" in
  full)
    include_performance=1
    include_release_control=1
    ;;
  integration) ;;
  *)
    echo "verify_all: --mode must be full or integration" >&2
    exit 2
    ;;
esac
api_port="${API_PORT:-8000}"
mock_vendor_port="${MOCK_VENDOR_PORT:-9028}"
web_port="${WEB_PORT:-18080}"
STEP(){ printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }
timing_file="${G2_TIMING_FILE:-}"
if [ -n "$timing_file" ]; then
  : > "$timing_file"
fi
run_stage(){
  local stage="$1" name="$2" stage_function="$3"
  local started_ns ended_ns duration_ms stage_status status record_status=0
  STEP "${stage}/10 ${name}"
  started_ns="$(python3 -c 'import time; print(time.monotonic_ns())')"
  set +e
  ( set -euo pipefail; "$stage_function" )
  stage_status=$?
  set -e
  ended_ns="$(python3 -c 'import time; print(time.monotonic_ns())')"
  duration_ms=$(((ended_ns - started_ns) / 1000000))
  status="success"
  if [ "$stage_status" -ne 0 ]; then
    status="failure"
  fi
  printf 'G2_TIMING stage=%s status=%s duration_ms=%s\n' \
    "$stage" "$status" "$duration_ms"
  if [ -n "$timing_file" ]; then
    set +e
    python3 scripts/g2_timing.py record \
      --file "$timing_file" \
      --stage "$stage" \
      --name "$name" \
      --status "$status" \
      --duration-ms "$duration_ms"
    record_status=$?
    set -e
  fi
  if [ "$stage_status" -ne 0 ]; then
    return "$stage_status"
  fi
  return "$record_status"
}
compose(){
  SMS_PLATFORM_ROOT="$ROOT" \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT="${SMS_RUNTIME_ROOT:-${TMPDIR:-/tmp}/sms-platform-${UID}/secrets}" \
  CALLBACK_EGRESS_ALLOWED_PORTS="${CALLBACK_EGRESS_ALLOWED_PORTS:-80,443,9028}" \
  COMPOSE_PROFILES=dev \
    "$ROOT/deploy/sms-compose" "$@"
}
frontend_gate(){
  local -a docker_args=(
    run --rm -v "$PWD/frontend:/app" -v /app/node_modules -w /app
  )
  if [ -n "${G2_NPM_CACHE_DIR:-}" ]; then
    mkdir -p "$G2_NPM_CACHE_DIR"
    docker_args+=(-v "$G2_NPM_CACHE_DIR:/root/.npm" -e npm_config_cache=/root/.npm)
  fi
  docker "${docker_args[@]}" node:24-alpine \
    sh -ec '
      npm ci --silent
      npm audit --audit-level=high
      npm run build:g2 & build_pid=$!
      npm run typecheck & typecheck_pid=$!
      npm test & test_pid=$!
      check_status=0
      wait "$build_pid" || check_status=$?
      wait "$typecheck_pid" || check_status=$?
      wait "$test_pid" || check_status=$?
      exit "$check_status"
    '
}
seed_dev(){
  local destination="deploy/secrets/dev-apikeys.txt"
  local temporary
  compose exec -T api \
    python -m app.cli seed-dev --keys-file /tmp/dev-apikeys.txt
  temporary="$(mktemp "${destination}.tmp.XXXXXX")"
  chmod 600 "$temporary"
  if ! compose exec -T api sh -ec \
    'exec dd if="$1" status=none' sh /tmp/dev-apikeys.txt > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  mv "$temporary" "$destination"
  chmod 600 "$destination"
}
wait_api_ready(){
  local attempt
  for attempt in $(seq 1 30); do
    if curl -sf "http://localhost:${api_port}/readyz"; then
      return 0
    fi
    sleep 2
  done
  echo "readyz 超时" >&2
  return 1
}
metrics_gate(){
  metrics_token="$(tr -d '\r\n' < deploy/secrets/metrics_scrape_token)"
  metrics_body="$(curl -sf -H "Authorization: Bearer ${metrics_token}" \
    "http://localhost:${api_port}/metrics")"
  for family in sms_queue_depth sms_send_rate_per_second sms_vendor_error_chunks \
    sms_uncertain_chunks sms_callback_failures sms_frequency_filtered_messages \
    sms_poll_lag_seconds sms_usage_projection_drift_dimensions \
    sms_usage_projection_drift_absolute_delta sms_metrics_snapshot_age_seconds; do
    printf '%s\n' "$metrics_body" | grep -q "^# TYPE ${family} gauge$" || {
      echo "Prometheus 指标缺失: ${family}" >&2
      exit 1
    }
  done
}

stage_0(){
python3 scripts/check_spec_consistency.py
python3 scripts/check_public_readiness.py
bash scripts/verify_vendor_live_test.sh
}

stage_1(){
( cd backend && uv run ruff check app migrations scripts_support tests \
    ../scripts/check_contract.py ../scripts/security_acceptance.py \
    ../scripts/verify_public_snapshot_cutover.py \
    ../scripts/verify_web_transport.py \
    ../scripts/g2_timing.py \
    ../scripts/e2e_api.py ../scripts/perf_smoke.py ../scripts/locustfile.py \
    && uv run mypy app migrations scripts_support ../scripts/check_contract.py \
    ../scripts/g2_timing.py \
    ../scripts/security_acceptance.py ../scripts/e2e_api.py ../scripts/perf_smoke.py \
    ../scripts/verify_web_transport.py \
    ../scripts/verify_public_snapshot_cutover.py )
}

stage_2(){
( cd backend && ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 uv run pytest -q \
    --cov=app/services --cov-report=term-missing --cov-fail-under=80 )
}

stage_3(){
( cd backend && ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 \
    uv run python scripts_support/check_migration.py )
}

stage_4(){
( cd backend && ENVIRONMENT=test DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 \
    uv run python ../scripts/check_contract.py ../openapi.yaml )
}

stage_5(){
compose down -v
if [ -n "${G2_DOCKER_CACHE_DIR:-}" ]; then
  bash scripts/build_g2_images.sh
  compose up -d
else
  compose up -d --build
fi
bash scripts/verify_redis_domains.sh
wait_api_ready
metrics_gate
compose exec -T api test ! -e /run/secrets/db_owner_password
bash scripts/verify_database_roles.sh
compose exec -T api test ! -e /run/secrets/redis_broker_password
compose exec -T worker-report test ! -e /run/secrets/audit_system_realtime_context_key
compose exec -T worker-report test ! -e /run/secrets/db_callback_password
compose exec -T worker-callback test ! -e /run/secrets/redis_auth_password
seed_dev
}

stage_6(){
python3 scripts/security_acceptance.py --base "http://localhost:${api_port}" --compose-file deploy/docker-compose.yml --secrets-dir deploy/secrets
}

stage_7(){
uv run --project backend python scripts/e2e_api.py --base "http://localhost:${api_port}" --mock-base "http://localhost:${mock_vendor_port}" --keys deploy/secrets/dev-apikeys.txt --compose-file deploy/docker-compose.yml
python3 scripts/verify_tls_termination_e2e.py \
  --project "$COMPOSE_PROJECT_NAME" \
  --web-port "$web_port" \
  --mock-password-file deploy/secrets/ldap_bind_password
}

stage_8(){
# E2E creates durable facts by design. Recreate only the G2 development volumes so
# the authoritative performance profile measures the same images from a clean DB.
compose down -v
compose up -d
wait_api_ready
seed_dev
uv run --project backend python scripts/perf_smoke.py --base "http://localhost:${api_port}" --mock-base "http://localhost:${mock_vendor_port}" --keys deploy/secrets/dev-apikeys.txt
}

stage_9(){
frontend_gate
}

stage_10(){
# Control-plane recovery smoke does not replace the Trivy release gate.
local image platform
local -a release_images=(
  sms-platform-api:local
  sms-platform-web:local
  sms-platform-postgres:local
  sms-platform-redis:local
)
for image in "${release_images[@]}"; do
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image" 2>/dev/null || true)"
  if [ "$platform" != "linux/amd64" ]; then
    printf 'G2_RELEASE_CONTROL candidates=rebuild reason=platform image=%s observed=%s\n' \
      "$image" "${platform:-unavailable}"
    bash scripts/verify_release_control.sh
    return
  fi
done
printf 'G2_RELEASE_CONTROL candidates=reuse source=stage-5 platform=linux/amd64\n'
SMS_RELEASE_CONTROL_API_IMAGE=sms-platform-api:local \
SMS_RELEASE_CONTROL_WEB_IMAGE=sms-platform-web:local \
SMS_RELEASE_CONTROL_POSTGRES_IMAGE=sms-platform-postgres:local \
SMS_RELEASE_CONTROL_REDIS_IMAGE=sms-platform-redis:local \
  bash scripts/verify_release_control.sh
}

if [[ "$mode" == full ]]; then
run_stage 0 "规格一致性与安全规则" stage_0
run_stage 1 "后端静态检查" stage_1
run_stage 2 "单元与集成测试" stage_2
run_stage 3 "迁移一致性" stage_3
run_stage 4 "完整契约一致性" stage_4
fi

cleanup(){ compose down -v; }
trap cleanup EXIT
run_stage 5 "干净整栈拉起" stage_5
run_stage 6 "运行态安全验收" stage_6
run_stage 7 "API 级 E2E" stage_7
if [[ "$include_performance" == 1 ]]; then
run_stage 8 "性能冒烟" stage_8
fi
if [[ "$mode" == full ]]; then
run_stage 9 "前端门禁" stage_9
fi
if [[ "$include_release_control" == 1 ]]; then
run_stage 10 "发布控制恢复烟测" stage_10
fi

echo -e "\n\033[1;32m✔ verify_all 全绿 mode=$mode performance=$include_performance release_control=$include_release_control\033[0m"
