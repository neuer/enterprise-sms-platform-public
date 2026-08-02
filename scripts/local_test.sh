#!/usr/bin/env bash
# 安全的一键本地测试栈：仅允许 dev mock 配置，不读取或打印任何 secret。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LOCAL_ENV_FILE:-$ROOT/.env}"
SECRETS_DIR="${LOCAL_SECRETS_DIR:-$ROOT/deploy/secrets}"
WEB_PORT="${WEB_PORT:-18180}"
API_PORT="${API_PORT:-18100}"
MOCK_VENDOR_PORT="${MOCK_VENDOR_PORT:-19128}"
LOCAL_HEALTH_ATTEMPTS="${LOCAL_HEALTH_ATTEMPTS:-60}"

fail() {
  printf '错误：%s\n' "$*" >&2
  return 1
}

usage() {
  cat <<EOF
用法：scripts/local_test.sh prepare|up|status|down|reset|help

  prepare 创建缺失的开发配置/secrets，但不启动容器
  up      创建缺失的开发配置/secrets，启动全栈并 seed
  status  查看容器与 Web/API/mock 健康状态
  down    停止本地栈，保留数据库与 Redis 卷
  reset   销毁本地测试卷并重新启动、seed
  help    显示本帮助

默认地址：
  Web:         http://localhost:${WEB_PORT}
  API:         http://localhost:${API_PORT}
  Mock vendor: http://localhost:${MOCK_VENDOR_PORT}

本地 mock 登录（密码从本机 0600 secret 文件读取）：
  admin01 / approver01 / operator01 / viewer01
  密码文件：${SECRETS_DIR}/ldap_bind_password

可在命令前设置 WEB_PORT、API_PORT、MOCK_VENDOR_PORT 覆盖默认端口。
EOF
}

env_value() {
  local key="$1"
  awk -v key="$key" '
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub("^[[:space:]]*" key "[[:space:]]*=[[:space:]]*", "")
      sub("[[:space:]]*#.*$", "")
      gsub("^[[:space:]]+|[[:space:]]+$", "")
      print
      exit
    }
  ' "$ENV_FILE"
}

ensure_dev_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    install -m 0600 "$ROOT/deploy/.env.example" "$ENV_FILE"
    printf '已创建本地开发配置：%s\n' "$ENV_FILE"
  fi
  chmod 0600 "$ENV_FILE"
}

validate_dev_env() {
  [[ -f "$ENV_FILE" ]] || fail "缺少 $ENV_FILE，请先执行 local_test.sh up"
  [[ "$(env_value ENVIRONMENT)" == "development" ]] || \
    fail "本地测试要求 ENVIRONMENT=development"
  [[ "$(env_value DEBUG)" == "1" ]] || fail "本地测试要求 DEBUG=1"
  [[ "$(env_value AUTH_MOCK)" == "1" ]] || fail "本地测试要求 AUTH_MOCK=1"
  [[ "$(env_value VENDOR_MOCK)" == "1" ]] || fail "本地测试要求 VENDOR_MOCK=1"
  [[ "$(env_value VENDOR_BASE_URL)" == "http://mock-vendor:9028" ]] || \
    fail "本地测试要求 VENDOR_BASE_URL=http://mock-vendor:9028"
}

create_text_secret() {
  local name="$1"
  local value="$2"
  local target="$SECRETS_DIR/$name"
  [[ -e "$target" ]] && { chmod 0600 "$target"; return; }
  local temporary="$SECRETS_DIR/.${name}.$$"
  printf '%s' "$value" > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$target"
}

create_random_secret() {
  local name="$1"
  local bytes="$2"
  local target="$SECRETS_DIR/$name"
  [[ -e "$target" ]] && { chmod 0600 "$target"; return; }
  local temporary="$SECRETS_DIR/.${name}.$$"
  openssl rand -base64 "$bytes" > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$target"
}

ensure_random_login_secret() {
  local target="$SECRETS_DIR/ldap_bind_password"
  if [[ -f "$target" ]]; then
    local length
    length="$(wc -c < "$target" | tr -d '[:space:]')"
    if [[ "$length" -ge 32 ]]; then
      chmod 0600 "$target"
      return
    fi
  fi
  local temporary="$SECRETS_DIR/.ldap_bind_password.$$"
  openssl rand -base64 32 > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$target"
}

ensure_dev_secrets() {
  install -d -m 0700 "$SECRETS_DIR"
  umask 077
  create_text_secret vendor_secret_name mock
  create_text_secret vendor_secret_key mock
  create_random_secret data_aes_key 32
  create_random_secret data_hmac_key 32
  create_random_secret jwt_secret 48
  ensure_random_login_secret
  create_random_secret metrics_scrape_token 48
  create_random_secret db_owner_password 32
  create_random_secret db_auth_password 32
  create_random_secret db_accept_password 32
  create_random_secret db_send_password 32
  create_random_secret db_callback_password 32
  create_random_secret db_export_password 32
  create_random_secret db_scheduler_password 32
  create_random_secret db_metrics_password 32
  create_random_secret redis_broker_password 32
  create_random_secret redis_auth_password 32
  create_random_secret redis_control_password 32
}

compose() {
  WEB_PORT="$WEB_PORT" API_PORT="$API_PORT" MOCK_VENDOR_PORT="$MOCK_VENDOR_PORT" \
  SMS_PLATFORM_ROOT="$ROOT" \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT="${SMS_RUNTIME_ROOT:-${TMPDIR:-/tmp}/sms-platform-${UID}/secrets}" \
  COMPOSE_PROFILES=dev \
    "$ROOT/deploy/sms-compose" "$@"
}

require_commands() {
  local command_name
  for command_name in docker curl openssl awk install; do
    command -v "$command_name" >/dev/null || fail "缺少主机命令：$command_name"
  done
  docker compose version >/dev/null
}

require_prepare_commands() {
  local command_name
  for command_name in openssl awk install; do
    command -v "$command_name" >/dev/null || fail "缺少主机命令：$command_name"
  done
}

prepare() {
  require_prepare_commands
  ensure_dev_env
  validate_dev_env
  ensure_dev_secrets
  # nginx(UID 101) 需要可写目录落盘脱敏访问日志；root 环境按固定属主收紧，
  # 普通开发账号回退为可写目录，避免 Docker 自动创建 root 属主目录导致 web 重启。
  local nginx_log_dir="$ROOT/deploy/security-report-nginx"
  install -d "$nginx_log_dir"
  if [[ "$(id -u)" == 0 ]]; then
    chown 101:101 "$nginx_log_dir"
    chmod 0750 "$nginx_log_dir"
  else
    chmod 0777 "$nginx_log_dir"
  fi
  printf '开发配置与 secrets 已准备完成\n'
}

wait_url() {
  local label="$1"
  local url="$2"
  local attempt
  for attempt in $(seq 1 "$LOCAL_HEALTH_ATTEMPTS"); do
    if curl -fsS "$url" >/dev/null; then
      printf '%s 已就绪\n' "$label"
      return 0
    fi
    sleep 2
  done
  fail "$label 健康检查超时：$url"
}

seed_dev() {
  local destination="$SECRETS_DIR/dev-apikeys.txt"
  local temporary
  if [[ -f "$SECRETS_DIR/dev-apikeys.txt" ]]; then
    chmod 0600 "$SECRETS_DIR/dev-apikeys.txt"
    compose exec -T api sh -ec \
      'umask 077; exec dd of="$1" status=none' sh /tmp/dev-apikeys.txt \
      < "$SECRETS_DIR/dev-apikeys.txt"
  fi
  compose exec -T api python -m app.cli seed-dev --keys-file /tmp/dev-apikeys.txt
  temporary="$(mktemp "${destination}.tmp.XXXXXX")"
  chmod 0600 "$temporary"
  if ! compose exec -T api sh -ec \
    'exec dd if="$1" status=none' sh /tmp/dev-apikeys.txt > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  mv "$temporary" "$destination"
  chmod 0600 "$destination"
}

print_ready() {
  printf '\n本地测试环境已就绪\n'
  printf '  Web 登录：    http://localhost:%s/login\n' "$WEB_PORT"
  printf '  API 存活：    http://localhost:%s/livez\n' "$API_PORT"
  printf '  API 就绪：    http://localhost:%s/readyz\n' "$API_PORT"
  printf '  Mock 状态：   http://localhost:%s/_mock/state\n' "$MOCK_VENDOR_PORT"
  printf '  用户名：      admin01 / approver01 / operator01 / viewer01\n'
  printf '  密码文件：    %s/ldap_bind_password（不回显）\n' "$SECRETS_DIR"
  printf '  停止命令：    scripts/local_test.sh down\n'
}

up() {
  require_commands
  ensure_dev_env
  validate_dev_env
  ensure_dev_secrets
  compose up -d --build
  wait_url API "http://localhost:${API_PORT}/readyz"
  wait_url 'Mock vendor' "http://localhost:${MOCK_VENDOR_PORT}/_mock/state"
  seed_dev
  print_ready
}

status() {
  require_commands
  compose ps
  printf '\n访问地址：Web http://localhost:%s/login · API http://localhost:%s · Mock http://localhost:%s\n' \
    "$WEB_PORT" "$API_PORT" "$MOCK_VENDOR_PORT"
  curl -fsS "http://localhost:${API_PORT}/livez" >/dev/null \
    && printf 'API process: alive\n' || printf 'API process: unavailable\n'
  curl -fsS "http://localhost:${API_PORT}/readyz" >/dev/null \
    && printf 'API traffic: ready\n' || printf 'API traffic: not ready\n'
  curl -fsS "http://localhost:${MOCK_VENDOR_PORT}/_mock/state" >/dev/null \
    && printf 'Mock vendor: healthy\n' || printf 'Mock vendor: unavailable\n'
}

down() {
  require_commands
  compose down
}

reset() {
  require_commands
  ensure_dev_env
  validate_dev_env
  ensure_dev_secrets
  compose down -v
  up
}

public_docker_session() {
  case "${1:-}" in
    up|status|down|reset)
      if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
        exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
      fi
      ;;
  esac
}

main() {
  case "${1:-help}" in
    prepare) prepare ;;
    up) up ;;
    status) status ;;
    down) down ;;
    reset) reset ;;
    help|-h|--help) usage ;;
    *)
      printf '不支持的命令：%s\n' "$1" >&2
      usage >&2
      return 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  public_docker_session "$@"
  main "$@"
fi
