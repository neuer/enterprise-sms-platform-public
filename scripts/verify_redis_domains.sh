#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
cd "$ROOT"

image="${REDIS_IMAGE:-sms-platform-redis:local}"
docker image inspect "$image" >/dev/null

suffix="$(date +%s)-$$"
domains=(broker auth control)
containers=()
volumes=()

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [[ "$status" -ne 0 ]]; then
    local container
    for container in "${containers[@]}"; do
      docker logs "$container" >&2 2>/dev/null || true
    done
  fi
  if [[ "${#containers[@]}" -gt 0 ]]; then
    docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  fi
  if [[ "${#volumes[@]}" -gt 0 ]]; then
    docker volume rm -f "${volumes[@]}" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

password_for() {
  printf 'redis-domain-probe-%s-%s-0123456789' "$1" "$suffix"
}

container_for() {
  printf 'sms-redis-domain-%s-%s' "$1" "$suffix"
}

allowed_key_for() {
  case "$1" in
    broker) printf 'realtime' ;;
    auth) printf 'auth:probe' ;;
    control) printf 'quota:probe' ;;
  esac
}

forbidden_key_for() {
  case "$1" in
    broker) printf 'auth:jwt:revoked:probe' ;;
    auth) printf 'queue:paused:realtime' ;;
    control) printf 'auth:jwt:revoked:probe' ;;
  esac
}

for domain in "${domains[@]}"; do
  container="$(container_for "$domain")"
  secret_volume="${container}-secrets"
  data_volume="${container}-data"
  containers+=("$container")
  volumes+=("$secret_volume" "$data_volume")
  docker volume create "$secret_volume" >/dev/null
  docker volume create "$data_volume" >/dev/null
  password="$(password_for "$domain")"
  printf '%s\n' "$password" | docker run --rm -i \
    --user 0:0 \
    --entrypoint sh \
    --mount "type=volume,source=${secret_volume},target=/run/secrets" \
    "$image" -ec "
      umask 077
      cat > /run/secrets/redis_${domain}_password
      chown 999:1000 /run/secrets/redis_${domain}_password
      chmod 0400 /run/secrets/redis_${domain}_password
    "
  docker run -d \
    --name "$container" \
    --mount "type=volume,source=${secret_volume},target=/run/secrets,readonly" \
    --mount "type=volume,source=${data_volume},target=/data" \
    --tmpfs /run/redis:rw,noexec,nosuid,nodev,size=1m,uid=999,gid=1000,mode=0700 \
    "$image" "$domain" >/dev/null
done

for domain in "${domains[@]}"; do
  container="$(container_for "$domain")"
  for _ in $(seq 1 30); do
    if docker exec "$container" redis-domain-healthcheck "$domain"; then
      break
    fi
    sleep 1
  done
  docker exec "$container" redis-domain-healthcheck "$domain"
  docker exec "$container" sh -ec \
    "! grep -Eq '(^| )~\\*( |$)' /run/redis/users.acl"
  docker exec "$container" sh -ec \
    "! grep -Eq '(^| )\\+@all( |$)' /run/redis/users.acl"

  if docker exec "$container" redis-cli -e ping >/dev/null 2>&1; then
    echo "Redis default user unexpectedly accepted a command" >&2
    exit 1
  fi

  password="$(password_for "$domain")"
  marker="isolated-${domain}"
  allowed_key="$(allowed_key_for "$domain")"
  forbidden_key="$(forbidden_key_for "$domain")"
  printf '%s\n' "$password" | docker exec -i "$container" \
    redis-cli --user "sms_${domain}" --askpass -e SET "$allowed_key" "$marker" \
    >/dev/null
  observed="$(
    printf '%s\n' "$password" | docker exec -i "$container" \
      redis-cli --user "sms_${domain}" --askpass --raw -e GET "$allowed_key"
  )"
  if [[ "$observed" != "$marker" ]]; then
    echo "Redis domain returned an unexpected isolation marker" >&2
    exit 1
  fi
  if [[ "$domain" == broker ]]; then
    celery_reply_key="probe.reply.celery.pidbox-${suffix}"
    printf '%s\n' "$password" | docker exec -i "$container" \
      redis-cli --user "sms_${domain}" --askpass -e SET "$celery_reply_key" "$marker" \
      >/dev/null
    observed="$(
      printf '%s\n' "$password" | docker exec -i "$container" \
        redis-cli --user "sms_${domain}" --askpass --raw -e GET "$celery_reply_key"
    )"
    if [[ "$observed" != "$marker" ]]; then
      echo "Redis broker rejected a scoped Celery pidbox reply key" >&2
      exit 1
    fi
  fi
  if printf '%s\n' "$password" | docker exec -i "$container" \
    redis-cli --user "sms_${domain}" --askpass -e SET "$forbidden_key" denied \
    >/dev/null 2>&1; then
    echo "Redis ACL unexpectedly allowed a cross-domain key" >&2
    exit 1
  fi
  if printf '%s\n' "$password" | docker exec -i "$container" \
    redis-cli --user "sms_${domain}" --askpass -e KEYS '*' >/dev/null 2>&1; then
    echo "Redis ACL unexpectedly allowed key enumeration" >&2
    exit 1
  fi
  if printf '%s\n' "$password" | docker exec -i "$container" \
    redis-cli --user "sms_${domain}" --askpass -e FLUSHALL >/dev/null 2>&1; then
    echo "Redis ACL unexpectedly allowed a dangerous command" >&2
    exit 1
  fi
  if docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" |
    grep -Fq "$password"; then
    echo "Redis password leaked into container environment" >&2
    exit 1
  fi
done

for domain in "${domains[@]}"; do
  container="$(container_for "$domain")"
  password="$(password_for "$domain")"
  allowed_key="$(allowed_key_for "$domain")"
  marker="isolated-${domain}"
  docker restart "$container" >/dev/null
  for _ in $(seq 1 30); do
    if docker exec "$container" redis-domain-healthcheck "$domain"; then
      break
    fi
    sleep 1
  done
  observed="$(
    printf '%s\n' "$password" | docker exec -i "$container" \
      redis-cli --user "sms_${domain}" --askpass --raw -e GET "$allowed_key"
  )"
  if [[ "$observed" != "$marker" ]]; then
    echo "Redis AOF restart did not preserve the domain marker" >&2
    exit 1
  fi
done

for source_domain in "${domains[@]}"; do
  password="$(password_for "$source_domain")"
  for target_domain in "${domains[@]}"; do
    [[ "$source_domain" == "$target_domain" ]] && continue
    target_container="$(container_for "$target_domain")"
    if printf '%s\n' "$password" | docker exec -i "$target_container" \
      redis-cli --user "sms_${target_domain}" --askpass -e PING >/dev/null 2>&1; then
      echo "Redis ACL unexpectedly accepted a credential from another domain" >&2
      exit 1
    fi
  done
done

echo "Redis 故障域验证通过: 独立最小 ACL、跨域拒绝、AOF 重启恢复、密码不入环境"
