#!/usr/bin/env bash
# Four-image release control smoke. This is control-plane evidence only and does
# not replace the Trivy release gate.
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$SCRIPT_ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

API_CANDIDATE_REF="${SMS_RELEASE_CONTROL_API_IMAGE:-}"
WEB_CANDIDATE_REF="${SMS_RELEASE_CONTROL_WEB_IMAGE:-}"
POSTGRES_CANDIDATE_REF="${SMS_RELEASE_CONTROL_POSTGRES_IMAGE:-}"
REDIS_CANDIDATE_REF="${SMS_RELEASE_CONTROL_REDIS_IMAGE:-}"

API_IMAGE_ID=""
WEB_IMAGE_ID=""
POSTGRES_IMAGE_ID=""
REDIS_IMAGE_ID=""

PARENT=""
PLATFORM=""
PROJECT=""
RELEASE_ROOT=""
RUNTIME_ROOT=""
SNAPSHOT_ROOT=""
API_PORT=""
WEB_PORT=""
MOCK_VENDOR_PORT=""
INGRESS_SUBNET=""
API_INGRESS_IPV4=""
WEB_INGRESS_IPV4=""
CANDIDATE_SHA=""
CURRENT_MIGRATION=""
STAGING_UID=""
CLEANUP_TAGS=()
CURRENT_API_REF=""
CURRENT_WEB_REF=""
CURRENT_POSTGRES_REF=""
CURRENT_REDIS_REF=""

fail() {
  printf 'release-control smoke failed: %s\n' "$*" >&2
  return 1
}

require_commands() {
  local command_name
  for command_name in docker git python3 openssl install cmp tr id chown; do
    command -v "$command_name" >/dev/null || fail "missing command: $command_name"
  done
  docker compose version >/dev/null
}

resolve_staging_uid() {
  STAGING_UID="$(id -u)"
  if [[ -n "${SUDO_UID:-}" ]]; then
    [[ "$SUDO_UID" =~ ^[0-9]+$ ]] || fail "staging SUDO_UID is invalid"
    STAGING_UID="$SUDO_UID"
  fi
}

set_staging_owner() {
  local current_uid
  current_uid="$(id -u)"
  if [[ "$current_uid" != "$STAGING_UID" ]]; then
    [[ "$current_uid" == 0 ]] || fail "staging owner cannot be changed safely"
    chown "$STAGING_UID" "$@"
  fi
}

snapshot_default_project() {
  local destination="$1"
  docker ps -a \
    --filter label=com.docker.compose.project=sms-platform \
    --format '{{.ID}} {{.Names}} {{.Image}}' | LC_ALL=C sort >"$destination.containers"
  docker volume ls \
    --filter label=com.docker.compose.project=sms-platform \
    --format '{{.Name}}' | LC_ALL=C sort >"$destination.volumes"
}

remove_smoke_objects() {
  local object
  if [[ -n "$PROJECT" ]]; then
    while IFS= read -r object; do
      [[ -z "$object" ]] || docker rm -f "$object" >/dev/null 2>&1 || true
    done < <(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
    while IFS= read -r object; do
      [[ -z "$object" ]] || docker volume rm -f "$object" >/dev/null 2>&1 || true
    done < <(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")
    while IFS= read -r object; do
      [[ -z "$object" ]] || docker network rm "$object" >/dev/null 2>&1 || true
    done < <(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT")
  fi
}

diagnose_failure() {
  [[ -n "$RELEASE_ROOT" && -d "$RELEASE_ROOT" ]] || return 0
  printf '%s\n' '--- release-control failure diagnostics (non-secret) ---' >&2
  python3 - "$RELEASE_ROOT" <<'PY' >&2
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
for state_path in sorted(root.glob("*/state.json")):
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    allowed = {
        key: state.get(key)
        for key in (
            "release_id",
            "state",
            "failure_type",
            "failure_step",
            "next_step",
            "interrupted_signal",
            "residual_changes",
        )
        if key in state
    }
    print(json.dumps(allowed, sort_keys=True))
    events_path = state_path.with_name("events.jsonl")
    try:
        events = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for event in events[-6:]:
        print(event)
PY
  if [[ -n "$PROJECT" ]]; then
    docker ps -a \
      --filter "label=com.docker.compose.project=$PROJECT" \
      --format '{{.Label "com.docker.compose.service"}} {{.ID}} {{.Image}} {{.Status}}' \
      | LC_ALL=C sort >&2 || true
  fi
}

cleanup() {
  local status=$?
  local cleanup_status=0
  trap - EXIT INT TERM HUP
  set +e
  if [[ "$status" -ne 0 ]]; then
    diagnose_failure
  fi
  if [[ -n "$PLATFORM" && -x "$PLATFORM/deploy/sms-compose" && -f "$PLATFORM/.env" ]]; then
    smoke_env "$PLATFORM/deploy/sms-compose" down --remove-orphans -v >/dev/null 2>&1 || true
  fi
  remove_smoke_objects
  local tag
  for tag in "${CLEANUP_TAGS[@]}"; do
    docker image rm "$tag" >/dev/null 2>&1 || true
  done
  if [[ -n "$PLATFORM" ]]; then
    git worktree remove --force "$PLATFORM" >/dev/null 2>&1 || cleanup_status=1
  fi
  if [[ -n "$PARENT" && "$PARENT" == "${TMPDIR:-/tmp}"/sms-platform-release-control-* ]]; then
    rm -rf -- "$PARENT"
  fi
  if [[ -n "$SNAPSHOT_ROOT" && -d "$SNAPSHOT_ROOT" ]]; then
    snapshot_default_project "$SNAPSHOT_ROOT/after"
    if ! cmp -s "$SNAPSHOT_ROOT/before.containers" "$SNAPSHOT_ROOT/after.containers" ||
       ! cmp -s "$SNAPSHOT_ROOT/before.volumes" "$SNAPSHOT_ROOT/after.volumes"; then
      printf 'release-control smoke changed the default sms-platform project\n' >&2
      cleanup_status=1
    fi
    rm -rf -- "$SNAPSHOT_ROOT"
  fi
  if [[ "$status" -eq 0 && "$cleanup_status" -ne 0 ]]; then
    status="$cleanup_status"
  fi
  exit "$status"
}

smoke_env() {
  env \
    API_PORT="$API_PORT" \
    WEB_PORT="$WEB_PORT" \
    MOCK_VENDOR_PORT="$MOCK_VENDOR_PORT" \
    SMS_PLATFORM_ROOT="$PLATFORM" \
    SMS_SECRETS_MODE=development \
    SMS_RELEASE_ROOT="$RELEASE_ROOT" \
    SMS_RUNTIME_ROOT="$RUNTIME_ROOT" \
    SMS_RELEASE_SMOKE=1 \
    COMPOSE_PROJECT_NAME="$PROJECT" \
    COMPOSE_PROFILES=dev \
    SMS_INGRESS_SUBNET="$INGRESS_SUBNET" \
    SMS_API_INGRESS_IPV4="$API_INGRESS_IPV4" \
    SMS_WEB_INGRESS_IPV4="$WEB_INGRESS_IPV4" \
    "$@"
}

release_prepare() {
  smoke_env "$PLATFORM/deploy/sms-compose" release prepare --manifest "$1"
}

release_activate() {
  smoke_env "$PLATFORM/deploy/sms-compose" release activate --release-id "$1"
}

release_activate_interruptibly() {
  export API_PORT WEB_PORT MOCK_VENDOR_PORT
  export SMS_PLATFORM_ROOT="$PLATFORM"
  export SMS_SECRETS_MODE=development
  export SMS_RELEASE_ROOT="$RELEASE_ROOT"
  export SMS_RUNTIME_ROOT="$RUNTIME_ROOT"
  export SMS_RELEASE_SMOKE=1
  export COMPOSE_PROJECT_NAME="$PROJECT"
  export COMPOSE_PROFILES=dev
  export SMS_INGRESS_SUBNET="$INGRESS_SUBNET"
  export SMS_API_INGRESS_IPV4="$API_INGRESS_IPV4"
  export SMS_WEB_INGRESS_IPV4="$WEB_INGRESS_IPV4"
  exec python3 "$PLATFORM/deploy/scripts/run_with_lifecycle_lock.py" \
    --runtime-root "$RUNTIME_ROOT" \
    --wrapper "$PLATFORM/deploy/sms-compose" \
    --operation release \
    -- activate --release-id "$1"
}

release_status() {
  smoke_env "$PLATFORM/deploy/sms-compose" release status --release-id "$1"
}

release_resume() {
  smoke_env "$PLATFORM/deploy/sms-compose" release resume --release-id "$1"
}

release_rollback() {
  smoke_env "$PLATFORM/deploy/sms-compose" release rollback --release-id "$1"
}

inspect_candidate() {
  local ref="$1"
  local destination="$2"
  local observed image_id platform extra
  observed="$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$ref")" ||
    fail "candidate image is unavailable: $ref"
  read -r image_id platform extra <<<"$observed"
  [[ -z "$extra" && "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    fail "candidate image identity is invalid: $ref"
  [[ "$platform" == linux/amd64 ]] ||
    fail "candidate image platform mismatch: $ref"
  printf -v "$destination" '%s' "$image_id"
}

prepare_candidates() {
  local suffix="$1"
  local override_count=0
  local ref
  for ref in \
    "$API_CANDIDATE_REF" "$WEB_CANDIDATE_REF" \
    "$POSTGRES_CANDIDATE_REF" "$REDIS_CANDIDATE_REF"; do
    [[ -z "$ref" ]] || override_count=$((override_count + 1))
  done

  if [[ "$override_count" -eq 0 ]]; then
    API_CANDIDATE_REF="sms-platform-control-${suffix}-api:candidate"
    WEB_CANDIDATE_REF="sms-platform-control-${suffix}-web:candidate"
    POSTGRES_CANDIDATE_REF="sms-platform-control-${suffix}-postgres:candidate"
    REDIS_CANDIDATE_REF="sms-platform-control-${suffix}-redis:candidate"
    CLEANUP_TAGS+=(
      "$API_CANDIDATE_REF" "$WEB_CANDIDATE_REF"
      "$POSTGRES_CANDIDATE_REF" "$REDIS_CANDIDATE_REF"
    )
    docker build --platform linux/amd64 -f backend/Dockerfile -t "$API_CANDIDATE_REF" "$ROOT"
    docker build --platform linux/amd64 -f frontend/Dockerfile -t "$WEB_CANDIDATE_REF" "$ROOT"
    docker build --platform linux/amd64 -f deploy/postgres.Dockerfile -t "$POSTGRES_CANDIDATE_REF" "$ROOT"
    docker build --platform linux/amd64 -f deploy/redis.Dockerfile -t "$REDIS_CANDIDATE_REF" "$ROOT"
  elif [[ "$override_count" -ne 4 ]]; then
    fail "candidate image overrides must be provided together"
  fi

  inspect_candidate "$API_CANDIDATE_REF" API_IMAGE_ID
  inspect_candidate "$WEB_CANDIDATE_REF" WEB_IMAGE_ID
  inspect_candidate "$POSTGRES_CANDIDATE_REF" POSTGRES_IMAGE_ID
  inspect_candidate "$REDIS_CANDIDATE_REF" REDIS_IMAGE_ID
}

tag_candidate() {
  local source="$1"
  local target="$2"
  docker image tag "$source" "$target"
  CLEANUP_TAGS+=("$target")
}

replace_env_refs() {
  local env_path="$1"
  local api_ref="$2"
  local web_ref="$3"
  local postgres_ref="$4"
  local redis_ref="$5"
  python3 - "$env_path" "$api_ref" "$web_ref" "$postgres_ref" "$redis_ref" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
refs = dict(zip(
    ("SMS_API_IMAGE", "SMS_WEB_IMAGE", "SMS_POSTGRES_IMAGE", "SMS_REDIS_IMAGE"),
    sys.argv[2:],
    strict=True,
))
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
seen = set()
rendered = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in refs:
        ending = "\n" if line.endswith("\n") else ""
        rendered.append(f"{key}={refs[key]}{ending}")
        seen.add(key)
    else:
        rendered.append(line)
if seen != set(refs):
    raise SystemExit("root env lacks the four image keys")
temporary = path.with_name(f".{path.name}.release-control.tmp")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(descriptor, "".join(rendered).encode())
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.replace(temporary, path)
os.chmod(path, 0o600)
PY
}

wait_for_runtime() {
  local service container state expected
  for _ in $(seq 1 180); do
    local ready=1
    for service in api web postgres redis redis-auth redis-control worker-realtime worker-bulk worker-callback outbox-dispatcher beat; do
      container="$(docker ps -q \
        --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=$service")"
      if [[ -z "$container" ]]; then
        ready=0
        break
      fi
      state="$(docker inspect --format '{{if (index .State "Health")}}{{(index .State "Health").Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
      case "$service" in
        api | web | postgres | redis | redis-auth | redis-control) expected=healthy ;;
        *) expected=running ;;
      esac
      if [[ "$state" != "$expected" ]]; then
        ready=0
        break
      fi
    done
    [[ "$ready" -eq 1 ]] && return 0
    sleep 1
  done
  fail "isolated Compose runtime did not become healthy"
}

observe_current_migration() {
  local observed
  observed="$(
    smoke_env "$PLATFORM/deploy/sms-compose" \
      exec -T postgres psql --no-psqlrc --tuples-only --no-align \
      --username sms_owner --dbname sms \
      --command 'SELECT version_num FROM alembic_version'
  )"
  if [[ "$observed" =~ ^([A-Za-z0-9_.-]{1,128})$ ]]; then
    CURRENT_MIGRATION="${BASH_REMATCH[1]}"
    return 0
  fi
  fail "isolated runtime migration head is invalid"
}

project_container_snapshot() {
  docker ps -a \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --format '{{.ID}} {{.Label "com.docker.compose.service"}} {{.Image}}' | LC_ALL=C sort
}

assert_status() {
  local release_id="$1"
  local expected="$2"
  local payload
  payload="$(release_status "$release_id")"
  python3 - "$expected" "$payload" <<'PY'
import json
import sys

expected, raw = sys.argv[1:]
state = json.loads(raw)
if state.get("state") != expected:
    raise SystemExit(f"unexpected release state: {state.get('state')}")
if state.get("release_gate_kind") == "release_control_smoke":
    if state.get("control_smoke_only") is not True:
        raise SystemExit("control_smoke_only marker is missing")
    if state.get("release_scan_performed") is not False:
        raise SystemExit("release_scan_performed marker is invalid")
PY
}

make_bundle() {
  local release_id="$1"
  local changed_csv="$2"
  local target_api="$3"
  local target_web="$4"
  local target_postgres="$5"
  local target_redis="$6"
  local include_data="$7"
  local bundle="$PARENT/staging/$release_id"
  install -d -m 0700 "$PARENT/staging" "$bundle"
  set_staging_owner "$PARENT/staging" "$bundle"

  local name ref
  for name in api web postgres redis; do
    case "$name" in
      api) ref="$target_api" ;;
      web) ref="$target_web" ;;
      postgres) ref="$target_postgres" ;;
      redis) ref="$target_redis" ;;
    esac
    if [[ ",$changed_csv," == *",$name,"* ]]; then
      docker image save --output "$bundle/$name-image.tar" "$ref"
      chmod 0600 "$bundle/$name-image.tar"
    fi
  done

  python3 "$PLATFORM/scripts/render_release_evidence.py" release-control-smoke \
    --output "$bundle/release-control-smoke.json" \
    --commit "$CANDIDATE_SHA" \
    --image api "$target_api" "$API_IMAGE_ID" linux/amd64 \
    --image web "$target_web" "$WEB_IMAGE_ID" linux/amd64 \
    --image postgres "$target_postgres" "$POSTGRES_IMAGE_ID" linux/amd64 \
    --image redis "$target_redis" "$REDIS_IMAGE_ID" linux/amd64

  if [[ "$include_data" == 1 ]]; then
    (
      cd "$PLATFORM"
      CANDIDATE_SHA="$CANDIDATE_SHA" \
        POSTGRES_IMAGE="$target_postgres" \
        REDIS_IMAGE="$target_redis" \
        bash scripts/verify_data_images.sh --report "$bundle/data-images.json"
    ) >&2
  fi

  python3 - \
    "$bundle" "$release_id" "$CANDIDATE_SHA" "$changed_csv" \
    "$target_api" "$target_web" "$target_postgres" "$target_redis" \
    "$API_IMAGE_ID" "$WEB_IMAGE_ID" "$POSTGRES_IMAGE_ID" "$REDIS_IMAGE_ID" \
    "$CURRENT_MIGRATION" "$include_data" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

(
    bundle_raw,
    release_id,
    commit,
    changed_raw,
    api_ref,
    web_ref,
    postgres_ref,
    redis_ref,
    api_id,
    web_id,
    postgres_id,
    redis_id,
    current_migration,
    include_data,
) = sys.argv[1:]
bundle = Path(bundle_raw)
changed = set(filter(None, changed_raw.split(",")))
refs = dict(api=api_ref, web=web_ref, postgres=postgres_ref, redis=redis_ref)
ids = dict(api=api_id, web=web_id, postgres=postgres_id, redis=redis_id)
images = {}
for name in ("api", "web", "postgres", "redis"):
    archive = bundle / f"{name}-image.tar"
    is_changed = name in changed
    images[name] = {
        "ref": refs[name],
        "id": ids[name],
        "archive_file": archive.name if is_changed else None,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()
        if is_changed
        else None,
        "changed": is_changed,
    }
manifest = {
    "schema_version": 1,
    "release_id": release_id,
    "commit": commit,
    "mode": "development",
    "images": images,
    "migration": {
        "from": current_migration,
        "target": current_migration,
        "compatibility": "none",
    },
    "evidence": {
        "release_gate_kind": "release_control_smoke",
        "release_gate": "release-control-smoke.json",
        "release_gate_sha256": hashlib.sha256(
            (bundle / "release-control-smoke.json").read_bytes()
        ).hexdigest(),
        "data_images": "data-images.json" if include_data == "1" else None,
        "backup_restore_change": None,
    },
}
path = bundle / "manifest.json"
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(descriptor, (json.dumps(manifest, sort_keys=True) + "\n").encode())
    os.fsync(descriptor)
finally:
    os.close(descriptor)
for child in bundle.iterdir():
    child.chmod(0o600)
PY
  local child
  for child in "$bundle"/*; do
    set_staging_owner "$child"
  done
  printf '%s\n' "$bundle/manifest.json"
}

main() {
  require_commands
  resolve_staging_uid
  [[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain --untracked-files=normal)" ]] ||
    fail "Git worktree must be clean before the smoke"
  CANDIDATE_SHA="$(git rev-parse HEAD)"

  SNAPSHOT_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sms-platform-release-snapshot-XXXXXXXX")"
  snapshot_default_project "$SNAPSHOT_ROOT/before"
  PARENT="$(python3 - <<'PY'
import secrets
import tempfile
from pathlib import Path

temporary = Path(tempfile.gettempdir())
for _ in range(32):
    candidate = temporary / f"sms-platform-release-control-{secrets.token_hex(8)}"
    try:
        candidate.mkdir(mode=0o700)
    except FileExistsError:
        continue
    print(candidate)
    break
else:
    raise SystemExit("unable to create a private release-control directory")
PY
)"
  PROJECT="$(basename "$PARENT")"
  [[ "$PROJECT" =~ ^sms-platform-release-control-[A-Za-z0-9]{8,32}$ ]] ||
    fail "temporary project name is unsafe"
  PLATFORM="$PARENT/platform"
  RELEASE_ROOT="$PARENT/releases"
  RUNTIME_ROOT="$PARENT/runtime-secrets"
  install -d -m 0700 "$RELEASE_ROOT" "$RUNTIME_ROOT"

  local port_data
  port_data="$(python3 - "$CANDIDATE_SHA" <<'PY'
import hashlib
import socket
import sys

ports = []
holders = []
for _ in range(3):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    holders.append(sock)
    ports.append(sock.getsockname()[1])
octet = 32 + (int(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:2], 16) % 180)
print(*ports, octet)
for sock in holders:
    sock.close()
PY
)"
  read -r API_PORT WEB_PORT MOCK_VENDOR_PORT subnet_octet <<<"$port_data"
  INGRESS_SUBNET="10.253.${subnet_octet}.0/24"
  API_INGRESS_IPV4="10.253.${subnet_octet}.2"
  WEB_INGRESS_IPV4="10.253.${subnet_octet}.3"

  git worktree add --detach "$PLATFORM" "$CANDIDATE_SHA" >/dev/null
  chmod 0755 "$PLATFORM/deploy/initdb/01-create-app-role.sh"
  (cd "$PLATFORM" && bash scripts/local_test.sh prepare)

  local suffix
  suffix="$(printf '%s' "${PROJECT#sms-platform-release-control-}" | tr '[:upper:]' '[:lower:]')"
  prepare_candidates "$suffix"
  local old_api="sms-platform-control-${suffix}-api:old"
  local old_web="sms-platform-control-${suffix}-web:old"
  local old_postgres="sms-platform-control-${suffix}-postgres:old"
  local old_redis="sms-platform-control-${suffix}-redis:old"
  tag_candidate "$API_CANDIDATE_REF" "$old_api"
  tag_candidate "$WEB_CANDIDATE_REF" "$old_web"
  tag_candidate "$POSTGRES_CANDIDATE_REF" "$old_postgres"
  tag_candidate "$REDIS_CANDIDATE_REF" "$old_redis"
  CURRENT_API_REF="$old_api"
  CURRENT_WEB_REF="$old_web"
  CURRENT_POSTGRES_REF="$old_postgres"
  CURRENT_REDIS_REF="$old_redis"
  replace_env_refs "$PLATFORM/.env" \
    "$CURRENT_API_REF" "$CURRENT_WEB_REF" "$CURRENT_POSTGRES_REF" "$CURRENT_REDIS_REF"

  smoke_env "$PLATFORM/deploy/sms-compose" up -d --remove-orphans --pull never
  wait_for_runtime
  observe_current_migration

  printf 'release-control: Web-only success path\n'
  local web_success="sms-platform-control-${suffix}-web:web-success"
  tag_candidate "$WEB_CANDIDATE_REF" "$web_success"
  local manifest
  manifest="$(make_bundle web-success web \
    "$CURRENT_API_REF" "$web_success" "$CURRENT_POSTGRES_REF" "$CURRENT_REDIS_REF" 0)"
  release_prepare "$manifest"
  release_activate web-success
  assert_status web-success succeeded
  CURRENT_WEB_REF="$web_success"

  printf 'release-control: API-only success path\n'
  local api_success="sms-platform-control-${suffix}-api:api-success"
  tag_candidate "$API_CANDIDATE_REF" "$api_success"
  manifest="$(make_bundle api-success api \
    "$api_success" "$CURRENT_WEB_REF" "$CURRENT_POSTGRES_REF" "$CURRENT_REDIS_REF" 0)"
  release_prepare "$manifest"
  release_activate api-success
  assert_status api-success succeeded
  CURRENT_API_REF="$api_success"

  printf 'release-control: data-image prepare path with verify_data_images.sh --report\n'
  local pg_data="sms-platform-control-${suffix}-postgres:data"
  local redis_data="sms-platform-control-${suffix}-redis:data"
  tag_candidate "$POSTGRES_CANDIDATE_REF" "$pg_data"
  tag_candidate "$REDIS_CANDIDATE_REF" "$redis_data"
  manifest="$(make_bundle data-prepare postgres,redis \
    "$CURRENT_API_REF" "$CURRENT_WEB_REF" "$pg_data" "$redis_data" 1)"
  release_prepare "$manifest"
  assert_status data-prepare prepared

  printf 'release-control: config failure leaves containers unchanged\n'
  local web_config="sms-platform-control-${suffix}-web:config-failure"
  tag_candidate "$WEB_CANDIDATE_REF" "$web_config"
  manifest="$(make_bundle config-failure web \
    "$CURRENT_API_REF" "$web_config" "$CURRENT_POSTGRES_REF" "$CURRENT_REDIS_REF" 0)"
  local containers_before containers_after
  containers_before="$(project_container_snapshot)"
  local env_backup="$PARENT/config-failure.env"
  install -m 0600 "$PLATFORM/.env" "$env_backup"
  printf '\nSMS_WEB_IMAGE=%s\n' "$CURRENT_WEB_REF" >>"$PLATFORM/.env"
  if release_prepare "$manifest" >/dev/null 2>&1; then
    install -m 0600 "$env_backup" "$PLATFORM/.env"
    fail "injected config failure unexpectedly succeeded"
  fi
  install -m 0600 "$env_backup" "$PLATFORM/.env"
  rm -f -- "$env_backup"
  containers_after="$(project_container_snapshot)"
  [[ "$containers_before" == "$containers_after" ]] ||
    fail "config failure changed isolated project containers"
  assert_status config-failure failed

  printf 'release-control: Web health failure compensates safely\n'
  local web_failure="sms-platform-control-${suffix}-web:health-failure"
  tag_candidate "$WEB_CANDIDATE_REF" "$web_failure"
  manifest="$(make_bundle web-health-failure web \
    "$CURRENT_API_REF" "$web_failure" "$CURRENT_POSTGRES_REF" "$CURRENT_REDIS_REF" 0)"
  release_prepare "$manifest"
  release_activate web-health-failure &
  local activation_pid=$!
  local target_container=""
  local observed_ref
  for _ in $(seq 1 300); do
    target_container="$(docker ps -aq \
      --filter "label=com.docker.compose.project=$PROJECT" \
      --filter label=com.docker.compose.service=web)"
    if [[ -n "$target_container" ]]; then
      observed_ref="$(docker inspect --format '{{.Config.Image}}' "$target_container" 2>/dev/null || true)"
      [[ "$observed_ref" == "$web_failure" ]] && break
    fi
    sleep 0.1
  done
  [[ -n "$target_container" && "$observed_ref" == "$web_failure" ]] ||
    fail "failed to observe target Web container"
  docker stop "$target_container" >/dev/null
  if wait "$activation_pid"; then
    fail "Web health failure unexpectedly succeeded"
  fi
  assert_status web-health-failure rolled_back
  wait_for_runtime

  printf 'release-control: TERM interruption resumes and reconciles\n'
  local api_interrupt="sms-platform-control-${suffix}-api:term-resume"
  tag_candidate "$API_CANDIDATE_REF" "$api_interrupt"
  manifest="$(make_bundle term-resume api \
    "$api_interrupt" "$CURRENT_WEB_REF" "$CURRENT_POSTGRES_REF" "$CURRENT_REDIS_REF" 0)"
  release_prepare "$manifest"
  release_activate_interruptibly term-resume &
  activation_pid=$!
  local state=""
  for _ in $(seq 1 300); do
    state="$(release_status term-resume 2>/dev/null | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("state", ""))' 2>/dev/null || true)"
    [[ "$state" == activating ]] && break
    sleep 0.1
  done
  [[ "$state" == activating ]] || fail "activation did not reach the interruptible state"
  kill -TERM "$activation_pid"
  if wait "$activation_pid"; then
    fail "TERM interruption unexpectedly returned success"
  fi
  release_resume term-resume
  assert_status term-resume succeeded
  CURRENT_API_REF="$api_interrupt"

  printf '%s\n' \
    'release-control smoke passed' \
    'gate_type=release_control_smoke' \
    'purpose=release_control_failure_injection' \
    'scan_performed=false' \
    'authorized_for_control_smoke=true' \
    'control_smoke_only=true' \
    'release_scan_performed=false' \
    'This does not replace the Trivy release gate.'
}

trap cleanup EXIT INT TERM HUP
main "$@"
