#!/usr/bin/env bash
# 使用一次性 PostgreSQL 固定真实数据库并发/租约恢复合同；不连接真实厂商。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi

tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/sms-vendor-postgres.XXXXXX")"
chmod 700 "$tmp_root"
container="sms-vendor-postgres-${UID}-$$"
redis_container="sms-vendor-auth-redis-${UID}-$$"
database="sms_vendor_recovery"
owner_password_file="$tmp_root/db_owner_password"
data_aes_key_file="$tmp_root/data_aes_key"
data_hmac_key_file="$tmp_root/data_hmac_key"
owner_password="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
data_aes_key="$(python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
data_hmac_key="$(python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
umask 077
printf '%s\n' "$owner_password" >"$owner_password_file"
printf '%s\n' "$data_aes_key" >"$data_aes_key_file"
printf '%s\n' "$data_hmac_key" >"$data_hmac_key_file"
# 外层目录保持仅当前操作者可遍历；文件使用 Docker secrets 的只读模式，
# 让容器内已降权的 postgres 用户能够读取只读 bind mount。
chmod 0444 "$owner_password_file"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker rm -f "$redis_container" >/dev/null 2>&1 || true
  rm -rf "$tmp_root"
}
trap cleanup EXIT

docker run --detach --name "$container" \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=512m \
  --publish 127.0.0.1::5432 \
  --env POSTGRES_DB="$database" \
  --env POSTGRES_USER=sms_owner \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/db_owner_password \
  --volume "$owner_password_file:/run/secrets/db_owner_password:ro" \
  --volume "$ROOT/deploy/initdb/01-create-app-role.sh:/docker-entrypoint-initdb.d/01-create-app-role.sh:ro" \
  postgres:16-alpine >/dev/null

ready=0
for _ in $(seq 1 90); do
  if docker exec "$container" pg_isready -U sms_owner -d "$database" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" = "1" ] || {
  container_status="$(docker inspect --format="{{.State.Status}}" "$container" 2>/dev/null || printf '%s' unknown)"
  printf '一次性 PostgreSQL 未就绪（容器状态：%s）\n' "$container_status" >&2
  docker logs "$container" >&2 || true
  exit 1
}

docker run --detach --name "$redis_container" \
  --publish 127.0.0.1::6379 \
  redis:7-alpine >/dev/null
redis_ready=0
for _ in $(seq 1 30); do
  if docker exec "$redis_container" redis-cli ping >/dev/null 2>&1; then
    redis_ready=1
    break
  fi
  sleep 1
done
[ "$redis_ready" = "1" ] || {
  printf '一次性 Redis 未就绪\n' >&2
  docker logs "$redis_container" >&2 || true
  exit 1
}
redis_port_mapping="$(docker port "$redis_container" 6379/tcp)"
case "$redis_port_mapping" in
  127.0.0.1:[0-9]*)
    redis_port="${redis_port_mapping#127.0.0.1:}"
    ;;
  *)
    printf '%s\n' "一次性 Redis 端口映射不安全" >&2
    exit 1
    ;;
esac

port_mapping="$(docker port "$container" 5432/tcp)"
case "$port_mapping" in
  127.0.0.1:[0-9]*)
    port="${port_mapping#127.0.0.1:}"
    ;;
  *)
    printf '%s\n' "一次性 PostgreSQL 端口映射不安全" >&2
    exit 1
    ;;
esac

(
  cd "$ROOT/backend"
  pytest_args=(-q)
  if [[ "${SMS_COVERAGE:-0}" == "1" ]]; then
    pytest_args+=(--cov=app --cov-report= --cov-append)
  fi
  ENVIRONMENT=test DEBUG=1 AUTH_MOCK=1 VENDOR_MOCK=1 \
  DB_HOST=127.0.0.1 \
  DB_PORT="$port" \
  DB_NAME="$database" \
  DB_OWNER_PASSWORD_FILE="$owner_password_file" \
  DATA_AES_KEY_FILE="$data_aes_key_file" \
  DATA_HMAC_KEY_FILE="$data_hmac_key_file" \
    uv run alembic upgrade head

  role_boundary_result="$(
    docker exec -i "$container" psql -qAt \
      -U sms_owner -d "$database" -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
UPDATE sys_config
SET value=CASE key
  -- 本测试只验证 RLS/SECURITY DEFINER 可见性，不解密凭据；使用非敏感的
  -- 合成信封标记，避免绕过生产中禁止 Webhook 明文落库的约束。
  WHEN 'alert_wecom_webhook' THEN 'sealed:v1:synthetic-non-secret'
  WHEN 'security_daily_resend_api_key' THEN 'synthetic-resend-key'
  WHEN 'alert_mail_to' THEN 'security@example.invalid'
  ELSE value
END
WHERE key IN (
  'alert_wecom_webhook','security_daily_resend_api_key','alert_mail_to'
);
SET ROLE sms_auth;
SELECT
  EXISTS(SELECT 1 FROM sys_config WHERE key='alert_wecom_webhook'),
  EXISTS(SELECT 1 FROM sys_config WHERE key='security_daily_resend_api_key'),
  (SELECT wecom_configured FROM alert_channel_availability()),
  (SELECT smtp_configured FROM alert_channel_availability());
RESET ROLE;
SET ROLE sms_callback;
SELECT
  EXISTS(SELECT 1 FROM sys_config WHERE key='alert_wecom_webhook'),
  EXISTS(SELECT 1 FROM sys_config WHERE key='security_daily_resend_api_key'),
  (SELECT wecom_configured FROM alert_channel_availability()),
  (SELECT smtp_configured FROM alert_channel_availability());
RESET ROLE;
SET ROLE sms_accept;
SELECT
  EXISTS(SELECT 1 FROM sys_config WHERE key='alert_wecom_webhook'),
  EXISTS(SELECT 1 FROM sys_config WHERE key='security_daily_resend_api_key');
RESET ROLE;
ROLLBACK;
SQL
  )"
  expected_role_boundary=$'f|f|t|t\nt|f|t|t\nt|t'
  if [[ "$role_boundary_result" != "$expected_role_boundary" ]]; then
    printf 'sys_config 敏感行运行角色隔离异常\n' >&2
    exit 1
  fi
  export_boundary_result="$(
    docker exec "$container" psql -qAt \
      -U sms_owner -d "$database" -v ON_ERROR_STOP=1 -c "
      SELECT
        has_column_privilege('sms_export','export_task','status','UPDATE'),
        has_column_privilege('sms_export','export_task','file_path','UPDATE'),
        NOT has_column_privilege('sms_export','export_task','decrypted','UPDATE'),
        NOT has_column_privilege('sms_export','export_task','filters','UPDATE'),
        NOT has_column_privilege('sms_export','export_task','scope_dept','UPDATE'),
        NOT has_column_privilege(
          'sms_export','export_task','creator_account_id','UPDATE'
        );"
  )"
  if [[ "$export_boundary_result" != "t|t|t|t|t|t" ]]; then
    printf 'export_task 授权元数据更新边界异常\n' >&2
    exit 1
  fi

  ENVIRONMENT=test DEBUG=1 AUTH_MOCK=1 VENDOR_MOCK=1 \
  VENDOR_UAT_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  EXPORT_AUTH_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  SECURITY_SESSION_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  OUTBOX_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  AUTH_GUARD_REDIS_URL="redis://127.0.0.1:${redis_port}/0" \
    uv run pytest "${pytest_args[@]}" \
      tests/integration/test_vendor_uat_recovery_postgres.py \
      tests/integration/test_export_authorization_postgres.py \
      tests/integration/test_security_session_postgres.py \
      tests/integration/test_atomic_password_change_postgres.py \
      tests/integration/test_auth_r2_postgres.py \
      tests/integration/test_daily_password_cas_postgres.py \
      tests/integration/test_auth_guard_redis.py \
      tests/integration/test_stable_principal_postgres.py \
      tests/integration/test_outbox_postgres.py \
      tests/integration/test_worker_fencing_postgres.py \
      tests/integration/test_vendor_event_facts_postgres.py \
      tests/integration/test_import_reservation_postgres.py \
      tests/integration/test_async_import_postgres.py \
      tests/integration/test_usage_ledger_postgres.py \
      tests/integration/test_raw_capture_legacy_postgres.py \
      tests/integration/test_raw_replay_eligibility_postgres.py \
      tests/integration/test_raw_replay_fencing_postgres.py \
      tests/integration/test_ops_audit_postgres.py \
      tests/integration/test_vendor_attempt_finalize_postgres.py \
      tests/integration/test_idempotency_claim_lease_postgres.py \
      tests/integration/test_inflight_ambiguous_commit_postgres.py \
      tests/integration/test_inflight_balance_conservation_postgres.py
)

printf '%s\n' "真实 PostgreSQL 恢复、稳定主体授权与安全会话合同通过：一次性数据库，仅合成数据"
