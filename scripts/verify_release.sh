#!/usr/bin/env bash
set -euo pipefail
umask 077

original_args=("$@")
report_path=""
sbom_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
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
      echo "用法: scripts/verify_release.sh [--report <absolute-path>] [--sbom-dir <absolute-directory>]" >&2
      exit 2
      ;;
  esac
done
for absolute_path in "$report_path" "$sbom_dir"; do
  if [ -n "$absolute_path" ]; then
    case "$absolute_path" in
      /*) ;;
      *) echo "发布证据路径必须使用绝对路径" >&2; exit 2 ;;
    esac
    if [ "$absolute_path" = "/" ]; then
      echo "发布证据路径无效" >&2
      exit 2
    fi
  fi
done
if [ -n "$sbom_dir" ] && [ -z "$report_path" ]; then
  echo "--sbom-dir 必须与 --report 同时使用" >&2
  exit 2
fi
if [ -n "$report_path" ]; then
  rm -f -- "$report_path"
fi

promoted_count=0
for promoted in \
  "${RELEASE_API_IMAGE:-}" \
  "${RELEASE_WEB_IMAGE:-}" \
  "${RELEASE_POSTGRES_IMAGE:-}" \
  "${RELEASE_REDIS_IMAGE:-}"; do
  if [ -n "$promoted" ]; then
    promoted_count=$((promoted_count + 1))
  fi
done
if [ "$promoted_count" -ne 0 ] && [ "$promoted_count" -ne 4 ]; then
  echo "最终发布镜像变量必须四项同时提供或全部省略" >&2
  exit 2
fi

docker_access="${SMS_DOCKER_ACCESS:-}"
if [ "$promoted_count" -eq 0 ]; then
  if [ -n "$docker_access" ] && [ "$docker_access" != "public" ]; then
    echo "verify-release: authenticated Docker access requires four promoted RepoDigests" >&2
    exit 2
  fi
  if [ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]; then
    script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
    exec "$script_root/scripts/docker_public.sh" run -- bash "$0" "${original_args[@]}"
  fi
else
  if [ "$docker_access" != "authenticated" ]; then
    echo "verify-release: promoted RepoDigest requires SMS_DOCKER_ACCESS=authenticated" >&2
    exit 2
  fi
  if [ "${SMS_DOCKER_PUBLIC_SESSION:-0}" = "1" ]; then
    echo "verify-release: promoted RepoDigest cannot use the public Docker session" >&2
    exit 2
  fi
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"

git diff --quiet
git diff --cached --quiet
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  echo "发布候选工作树不干净，拒绝构建或扫描" >&2
  exit 1
fi

candidate="$(git rev-parse HEAD)"
export DOCKER_BUILDKIT=1
export SOURCE_DATE_EPOCH=0
release_metadata="$(python3 scripts/release_metadata.py --root "$root")"
app_version="$(printf '%s' "$release_metadata" | python3 -c 'import json,sys; print(json.load(sys.stdin)["app_version"])')"
schema_revision="$(printf '%s' "$release_metadata" | python3 -c 'import json,sys; print(json.load(sys.stdin)["schema_revision"])')"
trivy_image="aquasec/trivy:0.70.0@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e"
scan_dir="$(mktemp -d "${TMPDIR:-/tmp}/sms-release-trivy.XXXXXX")"
chmod 0700 "$scan_dir"
cleanup() {
  rm -rf -- "$scan_dir"
}
trap cleanup EXIT

if [ "$promoted_count" -eq 0 ]; then
  if [ -n "$report_path" ] && [ -z "$sbom_dir" ]; then
    echo "候选发布证据必须提供 --sbom-dir" >&2
    exit 2
  fi
  if [ -n "$sbom_dir" ]; then
    if [ ! -d "$sbom_dir" ] || [ -L "$sbom_dir" ]; then
      echo "--sbom-dir 必须是已存在的安全目录" >&2
      exit 2
    fi
    for name in api web postgres redis; do
      rm -f -- \
        "$sbom_dir/$name.cdx.json" \
        "$sbom_dir/$name.cdx.json.part" \
        "$sbom_dir/$name.cdx.json.raw"
    done
  fi
  if [ -n "${RELEASE_SOURCE_REPORT:-}" ]; then
    echo "本地候选构建不得提供 RELEASE_SOURCE_REPORT" >&2
    exit 2
  fi
  api_image="sms-platform-release-api:${candidate}"
  web_image="sms-platform-release-web:${candidate}"
  postgres_image="sms-platform-release-postgres:${candidate}"
  redis_image="sms-platform-release-redis:${candidate}"
elif [ "$promoted_count" -eq 4 ]; then
  if [ -z "${RELEASE_SOURCE_REPORT:-}" ]; then
    echo "最终 RepoDigest 门禁必须提供 RELEASE_SOURCE_REPORT" >&2
    exit 2
  fi
  case "$RELEASE_SOURCE_REPORT" in
    /*) ;;
    *) echo "RELEASE_SOURCE_REPORT 必须使用绝对路径" >&2; exit 2 ;;
  esac
  if [ ! -f "$RELEASE_SOURCE_REPORT" ] || [ -L "$RELEASE_SOURCE_REPORT" ]; then
    echo "RELEASE_SOURCE_REPORT 不可用或不安全" >&2
    exit 2
  fi
  api_image="$RELEASE_API_IMAGE"
  web_image="$RELEASE_WEB_IMAGE"
  postgres_image="$RELEASE_POSTGRES_IMAGE"
  redis_image="$RELEASE_REDIS_IMAGE"
  for image in "$api_image" "$web_image" "$postgres_image" "$redis_image"; do
    if [[ ! "$image" =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]]; then
      echo "最终发布镜像必须使用 image@sha256:RepoDigest" >&2
      exit 2
    fi
    docker pull --platform linux/amd64 "$image"
  done
else
  echo "最终发布镜像变量必须四项同时提供或全部省略" >&2
  exit 2
fi

printf '发布候选 commit: %s\n' "$candidate"
if [ "$promoted_count" -eq 0 ]; then
  docker build -f backend/Dockerfile --pull --no-cache --provenance=false --sbom=false --platform linux/amd64 --build-arg SOURCE_DATE_EPOCH=0 --build-arg APP_VERSION="$app_version" --build-arg GIT_SHA="$candidate" --build-arg SCHEMA_REVISION="$schema_revision" -t "$api_image" .
  docker build -f frontend/Dockerfile --pull --no-cache --provenance=false --sbom=false --platform linux/amd64 --build-arg SOURCE_DATE_EPOCH=0 --build-arg APP_VERSION="$app_version" --build-arg GIT_SHA="$candidate" --build-arg SCHEMA_REVISION="$schema_revision" -t "$web_image" .
  docker build -f deploy/postgres.Dockerfile --pull --no-cache --provenance=false --sbom=false --platform linux/amd64 --build-arg SOURCE_DATE_EPOCH=0 --build-arg APP_VERSION="$app_version" --build-arg GIT_SHA="$candidate" --build-arg SCHEMA_REVISION="$schema_revision" -t "$postgres_image" .
  docker build -f deploy/redis.Dockerfile --pull --no-cache --provenance=false --sbom=false --platform linux/amd64 --build-arg SOURCE_DATE_EPOCH=0 --build-arg APP_VERSION="$app_version" --build-arg GIT_SHA="$candidate" --build-arg SCHEMA_REVISION="$schema_revision" -t "$redis_image" .
fi

scan_status=0
if [ "$promoted_count" -eq 0 ]; then
  for item in \
    "api|$api_image" \
    "web|$web_image" \
    "postgres|$postgres_image" \
    "redis|$redis_image"; do
    name="${item%%|*}"
    image="${item#*|}"
    printf '\n扫描镜像: %s\n' "$image"
    archive_name="${name}.tar"
    docker save --output "$scan_dir/$archive_name" "$image"
    if docker run --rm \
      -v "$scan_dir:/scan:ro" \
      -v trivycache:/root/.cache/trivy \
      "$trivy_image" image --input "/scan/$archive_name" \
      --scanners vuln \
      --severity HIGH,CRITICAL \
      --exit-code 1 \
      --no-progress \
      --format json > "$scan_dir/$name.json"; then
      :
    else
      status=$?
      if [ "$scan_status" -eq 0 ]; then
        scan_status="$status"
      fi
    fi
    if [ -n "$sbom_dir" ]; then
      sbom_raw="$sbom_dir/$name.cdx.json.raw"
      sbom_part="$sbom_dir/$name.cdx.json.part"
      if docker run --rm \
        -v "$scan_dir:/scan:ro" \
        -v trivycache:/root/.cache/trivy \
        "$trivy_image" image --input "/scan/$archive_name" \
        --format cyclonedx --no-progress > "$sbom_raw" \
        && python3 "$root/scripts/canonicalize_sbom.py" "$sbom_raw" "$sbom_part"; then
        rm -f -- "$sbom_raw"
        chmod 0600 "$sbom_part"
        mv "$sbom_part" "$sbom_dir/$name.cdx.json"
      else
        status=$?
        rm -f -- "$sbom_raw" "$sbom_part"
        if [ "$scan_status" -eq 0 ]; then
          scan_status="$status"
        fi
      fi
    fi
    rm -f -- "$scan_dir/$archive_name"
  done
else
  printf '\n复用候选 commit 的 Trivy 结果；最终 RepoDigest 必须保持相同 image ID。\n'
fi

inspect_repo_digests() {
  docker image inspect --format '{{json .RepoDigests}}' "$1" \
    | python3 -c 'import json, sys; digests = json.load(sys.stdin); print(",".join(digests or []))'
}

printf '\n发布镜像标识（归档时同时记录 registry push 后的 RepoDigest）:\n'
for image in "$api_image" "$web_image" "$postgres_image" "$redis_image"; do
  image_id="$(docker image inspect --format '{{.Id}}' "$image")"
  image_digests="$(inspect_repo_digests "$image")"
  printf '%s %s\n' "$image_id" "$image_digests"
done

if [ "$scan_status" -ne 0 ]; then
  exit "$scan_status"
fi

if [ -n "$report_path" ]; then
  api_id="$(docker image inspect --format '{{.Id}}' "$api_image")"
  web_id="$(docker image inspect --format '{{.Id}}' "$web_image")"
  postgres_id="$(docker image inspect --format '{{.Id}}' "$postgres_image")"
  redis_id="$(docker image inspect --format '{{.Id}}' "$redis_image")"
  api_digests="$(inspect_repo_digests "$api_image")"
  web_digests="$(inspect_repo_digests "$web_image")"
  postgres_digests="$(inspect_repo_digests "$postgres_image")"
  redis_digests="$(inspect_repo_digests "$redis_image")"
  if [ "$promoted_count" -eq 0 ]; then
    python3 "$root/scripts/render_release_evidence.py" release \
      --output "$report_path" \
      --commit "$candidate" \
      --trivy-image "$trivy_image" \
      --root "$root" \
      --workflow-repository "${GITHUB_REPOSITORY:-local}" \
      --workflow-run-id "${GITHUB_RUN_ID:-0}" \
      --workflow-run-attempt "${GITHUB_RUN_ATTEMPT:-0}" \
      --sbom api "$sbom_dir/api.cdx.json" \
      --sbom web "$sbom_dir/web.cdx.json" \
      --sbom postgres "$sbom_dir/postgres.cdx.json" \
      --sbom redis "$sbom_dir/redis.cdx.json" \
      --image api "$api_image" "$api_id" "$api_digests" "$scan_dir/api.json" \
      --image web "$web_image" "$web_id" "$web_digests" "$scan_dir/web.json" \
      --image postgres "$postgres_image" "$postgres_id" "$postgres_digests" "$scan_dir/postgres.json" \
      --image redis "$redis_image" "$redis_id" "$redis_digests" "$scan_dir/redis.json"
  else
    python3 "$root/scripts/render_release_evidence.py" promote \
      --output "$report_path" \
      --commit "$candidate" \
      --promotion-source "$RELEASE_SOURCE_REPORT" \
      --image api "$api_image" "$api_id" "$api_digests" \
      --image web "$web_image" "$web_id" "$web_digests" \
      --image postgres "$postgres_image" "$postgres_id" "$postgres_digests" \
      --image redis "$redis_image" "$redis_id" "$redis_digests"
  fi
fi

exit 0
