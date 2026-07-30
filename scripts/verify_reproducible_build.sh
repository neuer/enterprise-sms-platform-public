#!/usr/bin/env bash
set -euo pipefail
umask 077

original_args=("$@")
baseline=""
report_path=""
sbom_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --baseline)
      [ "$#" -ge 2 ] || { echo "--baseline 缺少路径" >&2; exit 2; }
      baseline="$2"
      shift 2
      ;;
    --report)
      [ "$#" -ge 2 ] || { echo "--report 缺少路径" >&2; exit 2; }
      report_path="$2"
      shift 2
      ;;
    --sbom-dir)
      [ "$#" -ge 2 ] || { echo "--sbom-dir 缺少路径" >&2; exit 2; }
      sbom_dir="$2"
      shift 2
      ;;
    *)
      echo "用法: scripts/verify_reproducible_build.sh --baseline <绝对路径> --report <绝对路径> --sbom-dir <绝对目录>" >&2
      exit 2
      ;;
  esac
done

for path in "$baseline" "$report_path" "$sbom_dir"; do
  case "$path" in
    /*) ;;
    *) echo "可复现构建证据路径必须使用绝对路径" >&2; exit 2 ;;
  esac
  [ "$path" != "/" ] || { echo "可复现构建证据路径无效" >&2; exit 2; }
done
if [ ! -f "$baseline" ] || [ -L "$baseline" ]; then
  echo "基准发布报告不可用或不安全" >&2
  exit 2
fi
if [ ! -d "$sbom_dir" ] || [ -L "$sbom_dir" ]; then
  echo "SBOM 目录不可用或不安全" >&2
  exit 2
fi

if [ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]; then
  script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  exec "$script_root/scripts/docker_public.sh" run -- bash "$0" "${original_args[@]}"
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"
git diff --quiet
git diff --cached --quiet
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  echo "发布候选工作树不干净，拒绝独立重建" >&2
  exit 1
fi

candidate="$(git rev-parse HEAD)"
export DOCKER_BUILDKIT=1
export SOURCE_DATE_EPOCH=0
metadata="$(python3 scripts/release_metadata.py --root "$root")"
app_version="$(printf '%s' "$metadata" | python3 -c 'import json,sys; print(json.load(sys.stdin)["app_version"])')"
schema_revision="$(printf '%s' "$metadata" | python3 -c 'import json,sys; print(json.load(sys.stdin)["schema_revision"])')"
trivy_image="aquasec/trivy:0.70.0@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/sms-release-rebuild.XXXXXX")"
chmod 0700 "$work_dir"
cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

rm -f -- "$report_path"
for name in api web postgres redis; do
  rm -f -- \
    "$sbom_dir/$name.rebuild.cdx.json" \
    "$sbom_dir/$name.rebuild.cdx.json.part" \
    "$sbom_dir/$name.rebuild.cdx.json.raw"
done

common_args=(
  --pull
  --no-cache
  --provenance=false
  --sbom=false
  --platform linux/amd64
  --build-arg SOURCE_DATE_EPOCH=0
  --build-arg "APP_VERSION=$app_version"
  --build-arg "GIT_SHA=$candidate"
  --build-arg "SCHEMA_REVISION=$schema_revision"
)
docker build -f backend/Dockerfile "${common_args[@]}" \
  -t "sms-platform-release-api:${candidate}" .
docker build -f frontend/Dockerfile "${common_args[@]}" \
  -t "sms-platform-release-web:${candidate}" .
docker build -f deploy/postgres.Dockerfile "${common_args[@]}" \
  -t "sms-platform-release-postgres:${candidate}" .
docker build -f deploy/redis.Dockerfile "${common_args[@]}" \
  -t "sms-platform-release-redis:${candidate}" .

python_args=(
  "$root/scripts/verify_reproducible_release.py"
  --baseline "$baseline"
  --output "$report_path"
  --commit "$candidate"
)
for name in api web postgres redis; do
  image="sms-platform-release-${name}:${candidate}"
  archive="$work_dir/$name.tar"
  raw="$sbom_dir/$name.rebuild.cdx.json.raw"
  part="$sbom_dir/$name.rebuild.cdx.json.part"
  canonical="$sbom_dir/$name.rebuild.cdx.json"
  docker save --output "$archive" "$image"
  docker run --rm \
    -v "$work_dir:/scan:ro" \
    -v trivycache:/root/.cache/trivy \
    "$trivy_image" image --input "/scan/$name.tar" \
    --format cyclonedx --no-progress > "$raw"
  python3 "$root/scripts/canonicalize_sbom.py" "$raw" "$part"
  rm -f -- "$raw" "$archive"
  chmod 0600 "$part"
  mv "$part" "$canonical"
  image_id="$(docker image inspect --format '{{.Id}}' "$image")"
  python_args+=(--sbom "$name" "$canonical" --image "$name" "$image_id")
done

python3 "${python_args[@]}"
