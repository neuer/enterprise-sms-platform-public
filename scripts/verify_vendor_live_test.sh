#!/usr/bin/env bash
# 真实厂商专项安全门禁：只运行 Mock/FakeCarrier 与合成数据测试，禁止网络探测。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
cd "$ROOT"

python3 scripts/check_invariants.py

(
  cd backend
  ENVIRONMENT=test DEBUG=1 AUTH_MOCK=1 VENDOR_MOCK=1 \
  VENDOR_BASE_URL=http://mock-vendor:9028 \
    uv run pytest -q \
      tests/test_settings.py \
      tests/test_auth_runtime.py \
      tests/test_audit_coverage.py \
      tests/test_cli.py \
      tests/test_compose_contract.py \
      tests/test_messages_api.py \
      tests/test_web_messages_api.py \
      tests/test_send_pipeline.py \
      tests/services/test_vendor_test_guard.py \
      tests/services/test_vendor_test_budget.py \
      tests/services/test_vendor_control_state.py \
      tests/services/test_vendor_test_recipient.py \
      tests/services/test_vendor_test_uat.py \
      tests/test_send_worker.py \
      tests/test_send_repository.py \
      tests/test_sql_service_repositories.py \
      tests/test_vendor_codes.py \
      tests/test_vendor_live_invariants.py \
      tests/test_vendor_test_budget_schema.py \
      tests/test_vendor_test_files.py \
      tests/test_vendor_test_manager.py \
      tests/test_install_vendor_credentials.py \
      tests/test_prepare_runtime_secrets.py \
      tests/test_systemd_deployment.py \
      tests/test_vendor_control_agent.py \
      tests/test_vendor_control_reload.py \
      tests/test_vendor_control_client.py \
      tests/test_vendor_control_journal.py \
      tests/test_vendor_control_protocol.py \
      tests/test_vendor_credential_store.py \
      tests/test_vendor_seal_sessions.py \
      tests/test_vendor_test_api.py \
      tests/test_vendor_test_bootstrap.py \
      tests/test_vendor_test_control_api.py \
      tests/test_vendor_test_operation.py \
      tests/test_vendor_test_operation_repository.py \
      tests/test_vendor_test_recipient_repository.py \
      tests/test_vendor_test_security_audit.py \
      tests/test_vendor_test_step_up.py \
      tests/test_vendor_test_uat_api.py \
      tests/test_vendor_test_web_console_invariants.py \
      tests/test_vendor_test_web_console_schema.py \
      tests/test_test_update_contract.py \
      tests/test_test_update_manager.py \
      tests/test_test_update_verify.py \
      tests/test_sms_compose_vendor_test.py
)

bash scripts/verify_vendor_postgres_recovery.sh

docker_args=(run --rm -v "$ROOT/frontend:/app" -v /app/node_modules -w /app)
if [ -n "${G2_NPM_CACHE_DIR:-}" ]; then
  mkdir -p "$G2_NPM_CACHE_DIR"
  docker_args+=(-v "$G2_NPM_CACHE_DIR:/root/.npm" -e npm_config_cache=/root/.npm)
fi
docker "${docker_args[@]}" node:24-alpine \
  sh -ec 'npm ci --silent && npm test -- \
    tests/vendor-seal.test.ts \
    tests/vendor-test-api.test.ts \
    tests/vendor-test-console.test.ts'

printf '真实厂商专项安全门禁通过：仅 Mock/FakeCarrier，无真实请求\n'
