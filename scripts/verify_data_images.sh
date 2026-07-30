#!/usr/bin/env bash
set -euo pipefail

report_path=""
if [ "$#" -eq 0 ]; then
  :
elif [ "$#" -eq 2 ] && [ "$1" = "--report" ]; then
  report_path="$2"
  case "$report_path" in
    /*) ;;
    *) echo "--report 必须使用绝对路径" >&2; exit 2 ;;
  esac
  if [ "$report_path" = "/" ]; then
    echo "--report 路径无效" >&2
    exit 2
  fi
else
  echo "用法: scripts/verify_data_images.sh [--report <absolute-path>]" >&2
  exit 2
fi
if [ -n "$report_path" ]; then
  rm -f -- "$report_path"
fi

root="$(git rev-parse --show-toplevel)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$root/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
cd "$root"

candidate="${CANDIDATE_SHA:-$(git rev-parse HEAD)}"
postgres_image="${POSTGRES_IMAGE:-sms-platform-release-postgres:${candidate}}"
redis_image="${REDIS_IMAGE:-sms-platform-release-redis:${candidate}}"

for image in "$postgres_image" "$redis_image"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "缺少数据服务候选镜像: $image" >&2
    exit 1
  fi
done

umask 077
tmpdir="$(mktemp -d)"
suffix="$(date +%s)-$$"
postgres_container="sms-pg-image-smoke-${suffix}"
redis_container="sms-redis-image-smoke-${suffix}"
postgres_volume="sms-pg-image-smoke-${suffix}"
postgres_secret_volume="sms-pg-image-smoke-secrets-${suffix}"
redis_volume="sms-redis-image-smoke-${suffix}"

cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ]; then
    for container in "$postgres_container" "$redis_container"; do
      if docker inspect "$container" >/dev/null 2>&1; then
        echo "--- $container logs ---" >&2
        docker logs "$container" >&2
      fi
    done
  fi
  docker rm -f "$postgres_container" "$redis_container" >/dev/null 2>&1
  docker volume rm -f \
    "$postgres_volume" "$postgres_secret_volume" "$redis_volume" >/dev/null 2>&1
  rm -rf "$tmpdir"
  exit "$status"
}
trap cleanup EXIT

openssl rand -hex 32 >"$tmpdir/db_owner_password"
docker volume create "$postgres_volume" >/dev/null
docker volume create "$postgres_secret_volume" >/dev/null
docker volume create "$redis_volume" >/dev/null

docker run --rm \
  --user 0:0 \
  --entrypoint sh \
  --mount "type=bind,source=${tmpdir},target=/source,readonly" \
  --mount "type=volume,source=${postgres_secret_volume},target=/run/secrets" \
  "$postgres_image" -ec '
    cp /source/db_owner_password /run/secrets/db_owner_password
    chown postgres:postgres /run/secrets/db_owner_password
    chmod 0400 /run/secrets/db_owner_password
  '

start_postgres() {
  docker run -d \
    --name "$postgres_container" \
    --mount "type=volume,source=${postgres_volume},target=/var/lib/postgresql/data" \
    --mount "type=volume,source=${postgres_secret_volume},target=/run/secrets,readonly" \
    --mount "type=bind,source=${root}/deploy/initdb/01-create-app-role.sh,target=/docker-entrypoint-initdb.d/01-create-app-role.sh,readonly" \
    -e POSTGRES_DB=sms_image_smoke \
    -e POSTGRES_USER=sms_owner \
    -e POSTGRES_PASSWORD_FILE=/run/secrets/db_owner_password \
    "$postgres_image" >/dev/null
}

wait_postgres() {
  for _ in $(seq 1 60); do
    if [ "$(
      docker exec "$postgres_container" psql -U sms_owner -d sms_image_smoke -Atc \
        "SELECT 1 FROM pg_roles WHERE rolname = 'sms_accept'" 2>/dev/null
    )" = "1" ]; then
      return 0
    fi
    sleep 1
  done
  echo "PostgreSQL 候选镜像未在 60 秒内就绪" >&2
  return 1
}

start_redis() {
  docker run -d \
    --name "$redis_container" \
    --mount "type=volume,source=${redis_volume},target=/data" \
    "$redis_image" redis-server --appendonly yes --appendfsync always >/dev/null
}

wait_redis() {
  for _ in $(seq 1 30); do
    if docker exec "$redis_container" redis-cli ping 2>/dev/null | grep -qx PONG; then
      return 0
    fi
    sleep 1
  done
  echo "Redis 候选镜像未在 30 秒内就绪" >&2
  return 1
}

start_postgres
wait_postgres
postgres_version_output="$(docker exec "$postgres_container" postgres --version)"
role_flags="$(
  docker exec "$postgres_container" psql -U sms_owner -d sms_image_smoke -Atc \
    "SELECT count(*)||'|'||bool_and(NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolinherit)
     FROM pg_roles WHERE rolname IN ('sms_auth','sms_accept','sms_send','sms_callback','sms_export','sms_scheduler','sms_metrics')"
)"
if [ "$role_flags" != "7|true" ]; then
  echo "数据库运行角色占位属性异常: $role_flags" >&2
  exit 1
fi
docker exec "$postgres_container" psql -U sms_owner -d sms_image_smoke -v ON_ERROR_STOP=1 -c \
  "CREATE TABLE image_smoke (value text PRIMARY KEY);
   INSERT INTO image_smoke VALUES ('persisted');
   GRANT USAGE ON SCHEMA public TO sms_accept;
   GRANT SELECT ON image_smoke TO sms_accept;" \
  >/dev/null
docker exec "$postgres_container" psql -U sms_owner -d sms_image_smoke -v ON_ERROR_STOP=1 -c \
  "SET ROLE sms_accept; SELECT value FROM image_smoke WHERE value = 'persisted';" >/dev/null
docker stop "$postgres_container" >/dev/null
docker rm "$postgres_container" >/dev/null

start_postgres
wait_postgres
postgres_marker="$(
  docker exec "$postgres_container" psql -U sms_owner -d sms_image_smoke -Atc \
    "SELECT value FROM image_smoke WHERE value = 'persisted'"
)"
if [ "$postgres_marker" != "persisted" ]; then
  echo "PostgreSQL 重启后未找到持久化标记" >&2
  exit 1
fi

start_redis
wait_redis
redis_version_output="$(docker exec "$redis_container" redis-server --version)"
docker exec "$redis_container" redis-cli SET image_smoke persisted >/dev/null
docker stop "$redis_container" >/dev/null
docker rm "$redis_container" >/dev/null

start_redis
wait_redis
redis_marker="$(docker exec "$redis_container" redis-cli GET image_smoke)"
if [ "$redis_marker" != "persisted" ]; then
  echo "Redis 重启后未找到 AOF 持久化标记" >&2
  exit 1
fi

if [ -n "$report_path" ]; then
  postgres_id="$(docker image inspect --format '{{.Id}}' "$postgres_image")"
  redis_id="$(docker image inspect --format '{{.Id}}' "$redis_image")"
  postgres_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$postgres_image")"
  redis_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$redis_image")"
  python3 "$root/scripts/render_release_evidence.py" data-images \
    --output "$report_path" \
    --commit "$candidate" \
    --image postgres "$postgres_image" "$postgres_id" "$postgres_platform" \
      "$postgres_version_output" \
    --image redis "$redis_image" "$redis_id" "$redis_platform" \
      "$redis_version_output"
fi

echo "数据镜像验证通过: PostgreSQL 角色/重启持久化, Redis AOF 重启持久化"
