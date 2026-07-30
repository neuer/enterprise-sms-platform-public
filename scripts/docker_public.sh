#!/usr/bin/env bash
# 在无桌面会话中为公开镜像操作创建一次性、空认证的 Docker CLI 会话。
set -euo pipefail
CALLER_UMASK="$(umask)"
umask 077

usage() {
  echo "usage: scripts/docker_public.sh run -- <command> [args...] | doctor" >&2
}

fail() {
  printf 'docker-public: %s\n' "$*" >&2
  return 1
}

resolve_docker_bin() {
  local resolved
  resolved="$(command -v docker 2>/dev/null)" || {
    fail "Docker CLI is unavailable"
    return 1
  }
  case "$resolved" in
    /*) ;;
    *)
      resolved="$(cd "$(dirname "$resolved")" && pwd -P)/$(basename "$resolved")"
      ;;
  esac
  [[ -x "$resolved" ]] || {
    fail "Docker CLI is unavailable"
    return 1
  }
  printf '%s\n' "$resolved"
}

resolve_endpoint() {
  local docker_bin="$1"
  local context endpoint
  if [[ -n "${DOCKER_HOST:-}" ]]; then
    endpoint="$DOCKER_HOST"
  else
    context="$("$docker_bin" context show 2>/dev/null)" || {
      fail "Docker context is unavailable"
      return 1
    }
    endpoint="$(
      "$docker_bin" context inspect "$context" \
        --format '{{.Endpoints.docker.Host}}' 2>/dev/null
    )" || {
      fail "Docker endpoint is unavailable"
      return 1
    }
  fi
  endpoint="${endpoint#\"}"
  endpoint="${endpoint%\"}"
  case "$endpoint" in
    unix:///*) ;;
    *)
      fail "local unix Docker endpoint is required"
      return 1
      ;;
  esac
  printf '%s\n' "$endpoint"
}

write_config() {
  local docker_bin="$1"
  local destination="$2"
  local plugins
  plugins="$(
    "$docker_bin" info --format '{{json .ClientInfo.Plugins}}' 2>/dev/null
  )" || {
    fail "Docker client plugin discovery failed"
    return 1
  }
  if ! python3 - "$destination" "$plugins" <<'PY'
import json
import os
import sys
from pathlib import Path

destination = Path(sys.argv[1])
try:
    plugins = json.loads(sys.argv[2])
except (TypeError, ValueError):
    raise SystemExit(2) from None

paths: dict[str, Path] = {}
if not isinstance(plugins, list):
    raise SystemExit(2)
for plugin in plugins:
    if not isinstance(plugin, dict):
        continue
    name = plugin.get("Name")
    path = plugin.get("Path")
    if name not in {"buildx", "compose"} or not isinstance(path, str):
        continue
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise SystemExit(2)
    paths[name] = candidate
if set(paths) != {"buildx", "compose"}:
    raise SystemExit(2)

directories = sorted({str(path.parent) for path in paths.values()})
payload = {
    "auths": {},
    "cliPluginsExtraDirs": directories,
    "credsStore": "sms-public",
}
destination.write_text(
    json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
    encoding="utf-8",
)
os.chmod(destination, 0o600)
PY
  then
    fail "Docker Compose/Buildx plugin unavailable"
    return 1
  fi
}

write_helpers() {
  local bin_dir="$1"
  install -d -m 0700 "$bin_dir"
  cat >"$bin_dir/docker-credential-sms-public" <<'SH'
#!/bin/sh
case "${1:-}" in
  get)
    IFS= read -r _ignored || true
    printf '%s\n' 'credentials not found in native keychain'
    exit 1
    ;;
  list)
    printf '%s\n' '{}'
    ;;
  store|erase)
    while IFS= read -r _ignored; do :; done
    printf '%s\n' 'docker-public: credential mutation is forbidden' >&2
    exit 1
    ;;
  *)
    printf '%s\n' 'docker-public: unsupported credential helper operation' >&2
    exit 2
    ;;
esac
SH
  cat >"$bin_dir/docker-credential-osxkeychain" <<'SH'
#!/bin/sh
if [ -n "${SMS_DOCKER_OSXKEYCHAIN_MARKER:-}" ]; then
  : >"$SMS_DOCKER_OSXKEYCHAIN_MARKER"
fi
printf '%s\n' 'docker-public: macOS Keychain access is forbidden' >&2
exit 97
SH
  cat >"$bin_dir/docker" <<'SH'
#!/bin/sh
set -eu
deny_registry_mutation() {
  printf '%s\n' \
    'docker-public: authenticated registry operation is forbidden' >&2
  exit 64
}
if [ "${1:-}" = "buildx" ] \
  && [ "${2:-}" = "imagetools" ] \
  && [ "${3:-}" = "create" ]; then
  deny_registry_mutation
fi
for argument in "$@"; do
  case "$argument" in
    login|logout|push|--push|--push=*|\
    --config|--config=*|--context|--context=*|--host|--host=*|-H|\
    --output=*type=registry*|type=registry*|\
    --output=*push=true*|type=image,*push=true*)
      deny_registry_mutation
      ;;
  esac
done
export DOCKER_CONFIG="${SMS_DOCKER_PUBLIC_CONFIG:?}"
export DOCKER_HOST="${SMS_DOCKER_PUBLIC_HOST:?}"
unset DOCKER_AUTH_CONFIG
unset REGISTRY_AUTH_FILE
exec "${SMS_DOCKER_PUBLIC_DOCKER_BIN:?}" "$@"
SH
  chmod 0500 \
    "$bin_dir/docker" \
    "$bin_dir/docker-credential-osxkeychain" \
    "$bin_dir/docker-credential-sms-public"
}

run_public() {
  local docker_bin endpoint session_dir bin_dir status
  docker_bin="$(resolve_docker_bin)" || return
  endpoint="$(resolve_endpoint "$docker_bin")" || return
  session_dir="$(mktemp -d "${TMPDIR:-/tmp}/sms-docker-public.XXXXXX")"
  chmod 0700 "$session_dir"
  bin_dir="$session_dir/bin"
  cleanup_public_session() {
    rm -rf -- "$session_dir"
  }
  trap cleanup_public_session EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  write_config "$docker_bin" "$session_dir/config.json"
  write_helpers "$bin_dir"

  if (
    unset DOCKER_AUTH_CONFIG
    unset REGISTRY_AUTH_FILE
    export DOCKER_CONFIG="$session_dir"
    export DOCKER_HOST="$endpoint"
    export PATH="$bin_dir:$PATH"
    export SMS_DOCKER_OSXKEYCHAIN_MARKER="$session_dir/osxkeychain-called"
    export SMS_DOCKER_PUBLIC_CONFIG="$session_dir"
    export SMS_DOCKER_PUBLIC_DOCKER_BIN="$docker_bin"
    export SMS_DOCKER_PUBLIC_HOST="$endpoint"
    export SMS_DOCKER_PUBLIC_SESSION=1
    umask "$CALLER_UMASK"
    "$@"
  ); then
    status=0
  else
    status=$?
  fi

  trap - EXIT INT TERM HUP
  cleanup_public_session
  return "$status"
}

doctor_inner() {
  docker version >/dev/null
  echo "docker-public: daemon PASS"
  docker compose version >/dev/null
  echo "docker-public: compose PASS"
  docker buildx version >/dev/null
  echo "docker-public: buildx PASS"
  docker buildx imagetools inspect docker.io/library/alpine:3.22 >/dev/null
  if [[ -e "${SMS_DOCKER_OSXKEYCHAIN_MARKER:?}" ]]; then
    fail "macOS Keychain helper was invoked"
    return 1
  fi
  echo "docker-public: registry-metadata PASS"
}

main() {
  local command="${1:-}"
  case "$command" in
    run)
      [[ "${2:-}" == "--" && "$#" -ge 3 ]] || {
        usage
        return 2
      }
      shift 2
      run_public "$@"
      ;;
    doctor)
      [[ "$#" -eq 1 ]] || {
        usage
        return 2
      }
      run_public "$0" _doctor
      echo "docker-public: cleanup PASS"
      ;;
    _doctor)
      [[ "$#" -eq 1 && "${SMS_DOCKER_PUBLIC_SESSION:-0}" == "1" ]] || {
        usage
        return 2
      }
      doctor_inner
      ;;
    *)
      usage
      return 2
      ;;
  esac
}

main "$@"
