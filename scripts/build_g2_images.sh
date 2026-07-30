#!/usr/bin/env bash
# 使用可丢弃 BuildKit 本地缓存构建 G2 的四个运行镜像。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
cache_root="${G2_DOCKER_CACHE_DIR:?G2_DOCKER_CACHE_DIR is required}"
mkdir -p "$cache_root"

builder="g2-${COMPOSE_PROJECT_NAME:-local}-$$"
builder="${builder//[^a-zA-Z0-9_.-]/-}"
builder_created=0
cleanup_builder(){
  if [ "$builder_created" -eq 1 ]; then
    docker buildx rm --force "$builder" >/dev/null 2>&1 || true
  fi
}
trap cleanup_builder EXIT

docker buildx create --driver docker-container --name "$builder" --use >/dev/null
builder_created=1
docker buildx inspect --bootstrap "$builder" >/dev/null

run_build(){
  local build_status
  set +e
  docker buildx build "$@"
  build_status=$?
  set -e
  return "$build_status"
}

rotate_cache(){
  local current="$1" next="$2" previous="${1}.previous"
  rm -rf "$previous"
  if [ -e "$current" ]; then
    mv "$current" "$previous"
  fi
  if mv "$next" "$current"; then
    rm -rf "$previous"
    return 0
  fi
  if [ -e "$previous" ]; then
    mv "$previous" "$current"
  fi
  return 1
}

build_image(){
  local cache_name="$1" image="$2" dockerfile="$3"
  local current="$cache_root/$cache_name"
  local next="$cache_root/${cache_name}.next"
  local previous="${current}.previous"
  local build_status
  local -a common=(
    --builder "$builder"
    --file "$dockerfile"
    --tag "$image"
    --load
    --cache-to "type=local,dest=$next,mode=max"
  )

  if [ ! -e "$current" ] && [ -e "$previous" ]; then
    mv "$previous" "$current"
  elif [ -e "$current" ]; then
    rm -rf "$previous"
  fi
  rm -rf "$next"

  if [ -f "$current/index.json" ]; then
    if ! run_build "${common[@]}" --cache-from "type=local,src=$current" "$ROOT"; then
      printf 'G2_BUILD_CACHE cache_name=%s status=retry-cold\n' "$cache_name"
      rm -rf "$next"
      if run_build "${common[@]}" "$ROOT"; then
        :
      else
        build_status=$?
        rm -rf "$next"
        return "$build_status"
      fi
    fi
  elif run_build "${common[@]}" "$ROOT"; then
    :
  else
    build_status=$?
    rm -rf "$next"
    return "$build_status"
  fi

  rotate_cache "$current" "$next"
  printf 'G2_BUILD_CACHE cache_name=%s status=success\n' "$cache_name"
}

build_names=(api web postgres redis)
build_pids=()

build_image api sms-platform-api:local backend/Dockerfile &
build_pids+=("$!")
build_image web sms-platform-web:local frontend/Dockerfile &
build_pids+=("$!")
build_image postgres sms-platform-postgres:local deploy/postgres.Dockerfile &
build_pids+=("$!")
build_image redis sms-platform-redis:local deploy/redis.Dockerfile &
build_pids+=("$!")

overall_status=0
for index in "${!build_pids[@]}"; do
  set +e
  wait "${build_pids[$index]}"
  build_status=$?
  set -e
  if [ "$build_status" -ne 0 ]; then
    printf 'G2_BUILD_CACHE cache_name=%s status=failure exit_code=%s\n' \
      "${build_names[$index]}" "$build_status" >&2
    if [ "$overall_status" -eq 0 ]; then
      overall_status="$build_status"
    fi
  fi
done
exit "$overall_status"
