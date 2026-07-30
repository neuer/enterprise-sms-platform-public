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
database="sms_vendor_recovery"
owner_password_file="$tmp_root/db_owner_password"
owner_password="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
umask 077
printf '%s\n' "$owner_password" >"$owner_password_file"
# 外层目录保持仅当前操作者可遍历；文件使用 Docker secrets 的只读模式，
# 让容器内已降权的 postgres 用户能够读取只读 bind mount。
chmod 0444 "$owner_password_file"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
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
    uv run alembic upgrade head

  ENVIRONMENT=test DEBUG=1 AUTH_MOCK=1 VENDOR_MOCK=1 \
  VENDOR_UAT_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  EXPORT_AUTH_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  SECURITY_SESSION_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
  OUTBOX_POSTGRES_DSN="postgresql+asyncpg://sms_owner:${owner_password}@127.0.0.1:${port}/${database}" \
    uv run pytest "${pytest_args[@]}" \
      tests/integration/test_vendor_uat_recovery_postgres.py \
      tests/integration/test_export_authorization_postgres.py \
      tests/integration/test_security_session_postgres.py \
      tests/integration/test_atomic_password_change_postgres.py \
      tests/integration/test_stable_principal_postgres.py \
      tests/integration/test_outbox_postgres.py \
      tests/integration/test_worker_fencing_postgres.py \
      tests/integration/test_vendor_event_facts_postgres.py \
      tests/integration/test_import_reservation_postgres.py \
      tests/integration/test_async_import_postgres.py \
      tests/integration/test_usage_ledger_postgres.py
)

printf '%s\n' "真实 PostgreSQL 恢复、稳定主体授权与安全会话合同通过：一次性数据库，仅合成数据"
