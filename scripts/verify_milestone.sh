#!/usr/bin/env bash
# scripts/verify_milestone.sh — G1 分阶段门禁；完整 G2 使用 verify_all.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

milestone="${1:-}"
api_port="${API_PORT:-8000}"
case "$milestone" in
  M0|M1|M2|M3|M4) ;;
  *) echo "用法: bash scripts/verify_milestone.sh M0|M1|M2|M3|M4" >&2; exit 2 ;;
esac
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi

step() { printf '\n\033[1;36m── %s / %s ──\033[0m\n' "$milestone" "$*"; }
require_file() { [ -f "$1" ] || { echo "缺少文件: $1" >&2; exit 1; }; }
require_dir() { [ -d "$1" ] || { echo "缺少目录: $1" >&2; exit 1; }; }
compose() {
  SMS_PLATFORM_ROOT="$ROOT" \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT="${SMS_RUNTIME_ROOT:-${TMPDIR:-/tmp}/sms-platform-${UID}/secrets}" \
  COMPOSE_PROFILES=dev \
    "$ROOT/deploy/sms-compose" "$@"
}
frontend_gate() {
  docker run --rm -v "$PWD/frontend:/app" -v /app/node_modules \
    -w /app node:24-alpine \
    sh -ec 'npm ci --silent && npm audit --audit-level=high && npm run build && npm test'
}
seed_dev() {
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
metrics_gate() {
  metrics_token="$(tr -d '\r\n' < deploy/secrets/metrics_scrape_token)"
  metrics_body="$(curl -sf -H "Authorization: Bearer ${metrics_token}" \
    "http://localhost:${api_port}/metrics")"
  for family in sms_queue_depth sms_send_rate_per_second sms_vendor_error_chunks \
    sms_uncertain_chunks sms_callback_failures sms_frequency_filtered_messages \
    sms_poll_lag_seconds; do
    printf '%s\n' "$metrics_body" | grep -q "^# TYPE ${family} gauge$" || {
      echo "Prometheus 指标缺失: ${family}" >&2
      exit 1
    }
  done
}

step "规格与仓库预检"
git rev-parse --is-inside-work-tree >/dev/null
python3 scripts/check_spec_consistency.py
python3 scripts/check_public_readiness.py
git check-ignore -q .env
git check-ignore -q deploy/secrets/dev-apikeys.txt

step "M0 基础资产"
require_dir backend
require_dir frontend
require_file backend/Dockerfile
require_file frontend/Dockerfile
require_file frontend/package-lock.json
require_file deploy/nginx.conf
require_file scripts/check_contract.py
require_file backend/scripts_support/check_migration.py
require_file scripts/check_invariants.py
compose config >/dev/null
( cd backend && uv run ruff check app migrations scripts_support tests ../scripts/check_contract.py \
    && uv run mypy app migrations scripts_support ../scripts/check_contract.py \
    && uv run pytest -q )
frontend_gate

step "Compose 启动与基础安全验收"
cleanup() { compose down; }
trap cleanup EXIT
if [ "$milestone" = "M0" ]; then
  compose up -d --build postgres redis migrate api web
else
  compose up -d --build
fi
for i in $(seq 1 45); do
  if curl -sf "http://localhost:${api_port}/readyz" >/dev/null; then break; fi
  sleep 2
  [ "$i" = 45 ] && { echo "readyz 超时" >&2; exit 1; }
done
compose exec -T api test ! -e /run/secrets/db_owner_password
bash scripts/verify_database_roles.sh

if [ "$milestone" != "M0" ]; then
  step "M1+ 迁移、契约、覆盖率与安全规则"
  ( cd backend && uv run python scripts_support/check_migration.py )
  if [ "$milestone" = "M4" ]; then
    metrics_gate
    ( cd backend && uv run python ../scripts/check_contract.py ../openapi.yaml )
  else
    ( cd backend && uv run python ../scripts/check_contract.py ../openapi.yaml --allow-missing )
  fi
  ( cd backend && uv run pytest -q --cov=app/services --cov-report=term-missing --cov-fail-under=80 )
  python3 scripts/check_invariants.py
  seed_dev
fi

printf '\n\033[1;32m✔ %s 里程碑门禁全绿\033[0m\n' "$milestone"
