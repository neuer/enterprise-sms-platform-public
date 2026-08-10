#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi
CONFIG_FILE="$ROOT/.env.test-update"
CONFIG_TARGET=""
CONFIG_PORT=""
CONFIG_VENDOR_ORIGIN=""
if [[ -e "$CONFIG_FILE" ]]; then
  [[ -f "$CONFIG_FILE" && ! -L "$CONFIG_FILE" && -O "$CONFIG_FILE" ]] || {
    echo "test-update: .env.test-update 必须是当前用户拥有的普通文件" >&2
    exit 2
  }
  if mode="$(stat -f '%Lp' "$CONFIG_FILE" 2>/dev/null)"; then
    :
  else
    mode="$(stat -c '%a' "$CONFIG_FILE" 2>/dev/null || true)"
  fi
  [[ "$mode" == 400 || "$mode" == 600 ]] || {
    echo "test-update: .env.test-update 权限必须是 400 或 600" >&2
    exit 2
  }
  seen_target=0
  seen_port=0
  seen_vendor_origin=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" != *$'\r'* ]] || {
      echo "test-update: .env.test-update 格式无效" >&2
      exit 2
    }
    case "$line" in
      "" | \#*) continue ;;
      SMS_TEST_UPDATE_TARGET=*)
        [[ "$seen_target" == 0 ]] || {
          echo "test-update: .env.test-update 存在重复键" >&2
          exit 2
        }
        CONFIG_TARGET="${line#*=}"
        seen_target=1
        ;;
      SMS_TEST_UPDATE_PORT=*)
        [[ "$seen_port" == 0 ]] || {
          echo "test-update: .env.test-update 存在重复键" >&2
          exit 2
        }
        CONFIG_PORT="${line#*=}"
        seen_port=1
        ;;
      SMS_VENDOR_LIVE_TEST_ORIGIN=*)
        [[ "$seen_vendor_origin" == 0 ]] || {
          echo "test-update: .env.test-update 存在重复键" >&2
          exit 2
        }
        CONFIG_VENDOR_ORIGIN="${line#*=}"
        seen_vendor_origin=1
        ;;
      *)
        echo "test-update: .env.test-update 只允许 TARGET、PORT 与 VENDOR_LIVE_TEST_ORIGIN" >&2
        exit 2
        ;;
    esac
  done <"$CONFIG_FILE"
fi
TARGET="${SMS_TEST_UPDATE_TARGET:-$CONFIG_TARGET}"
CANONICAL_REMOTE_ROOT="/opt/sms-platform"
REMOTE_ROOT="${SMS_TEST_UPDATE_ROOT:-$CANONICAL_REMOTE_ROOT}"
PORT="${SMS_TEST_UPDATE_PORT:-${CONFIG_PORT:-22}}"
VENDOR_ORIGIN="${SMS_VENDOR_LIVE_TEST_ORIGIN:-$CONFIG_VENDOR_ORIGIN}"
REF="origin/main"
COMMAND="apply"
REMOTE_SMS_COMPOSE="/usr/local/sbin/sms-compose"
BOOTSTRAP_SMS_COMPOSE="/usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap"
HOST_CONTROL_PATHS=(
  "deploy/scripts/install_test_secure_access.py"
  "deploy/scripts/test_secure_access_contract.py"
  "deploy/scripts/test_secure_access_runtime.py"
  "deploy/scripts/test_secure_access_manager.py"
  "deploy/scripts/render_trusted_proxy_conf.py"
  "deploy/scripts/vendor_test_files.py"
  "deploy/scripts/check_test_update_migration.py"
  "deploy/scripts/public_baseline_activation.py"
  "deploy/scripts/public_baseline_manager.py"
  "deploy/scripts/public_cutover_bootstrap.py"
  "deploy/scripts/run_with_lifecycle_lock.py"
  "deploy/scripts/test_update_apply.py"
  "deploy/scripts/test_update_backup.py"
  "deploy/scripts/test_update_contract.py"
  "deploy/scripts/test_update_manager.py"
  "deploy/scripts/test_update_promote.py"
  "deploy/scripts/test_update_store.py"
  "deploy/scripts/test_update_verify.py"
  "scripts/check_public_readiness.py"
  "scripts/export_public_snapshot.py"
  "scripts/verify_public_snapshot_cutover.py"
  "deploy/sms-compose"
  "deploy/trusted-proxies.conf"
  "deploy/systemd/sms-platform-test-secure-access.service"
)
REMOTE_CONTROL_ENV=(
  "/usr/bin/env"
  "SMS_PLATFORM_ROOT=$REMOTE_ROOT"
  "SMS_SECRETS_MODE=development"
  "SMS_RUNTIME_ROOT=/run/sms-platform/secrets"
  "SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials"
)
if [[ -n "$VENDOR_ORIGIN" ]]; then
  [[ "$VENDOR_ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || {
    echo "test-update: SMS_VENDOR_LIVE_TEST_ORIGIN 格式无效" >&2
    exit 2
  }
  REMOTE_CONTROL_ENV+=("SMS_VENDOR_LIVE_TEST_ORIGIN=$VENDOR_ORIGIN")
fi

usage() {
  echo "usage: scripts/test_update.sh [plan|build|apply|rebaseline|recover-rebaseline-verify|status|promote] [--ref origin/BRANCH]" >&2
}

github_repository_identity() {
  python3 -c 'import re,sys,urllib.parse
raw=sys.argv[1]
if raw.startswith("git@github.com:"):
 path=raw.removeprefix("git@github.com:")
elif raw.startswith(("https://","ssh://")):
 parsed=urllib.parse.urlsplit(raw)
 if parsed.hostname != "github.com" or parsed.password is not None:
  raise SystemExit(1)
 if parsed.scheme == "https" and parsed.username is not None:
  raise SystemExit(1)
 if parsed.scheme == "ssh" and parsed.username != "git":
  raise SystemExit(1)
 path=parsed.path
else:
 raise SystemExit(1)
path=path.strip("/")
if path.endswith(".git"):
 path=path[:-4]
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",path):
 raise SystemExit(1)
print(path.lower())' "$1"
}

remote_sms_compose() {
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" sudo "${REMOTE_CONTROL_ENV[@]}" "$REMOTE_SMS_COMPOSE" "$@"
}

remote_bootstrap_sms_compose() {
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" sudo "${REMOTE_CONTROL_ENV[@]}" "$BOOTSTRAP_SMS_COMPOSE" "$@"
}

remote_git_read() {
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" git -C "$REMOTE_ROOT" "$@"
}

remote_git_preflight() {
  local output
  if ! output="$(remote_git_read "$@" 2>/dev/null)"; then
    echo "test-update: 远端 Git 基线不可由更新用户读取；请检查 /opt/sms-platform/.git 对 operator 的读/遍历权限" >&2
    return 1
  fi
  printf '%s\n' "$output"
}

verify_operator_git_after_switch() {
  local phase="$1"
  local origin repository commit
  origin="$(remote_git_preflight remote get-url origin)"
  if ! repository="$(github_repository_identity "$origin")" ||
    [[ "$repository" != "$LOCAL_REPOSITORY" ]]; then
    echo "test-update: ${phase} 后 operator Git origin 与本地仓库不一致" >&2
    return 1
  fi
  commit="$(remote_git_preflight rev-parse HEAD)"
  if [[ "$commit" != "$TARGET_COMMIT" ]]; then
    echo "test-update: ${phase} 后 operator Git HEAD 与目标 commit 不一致" >&2
    return 1
  fi
  if ! remote_git_preflight status --porcelain=v1 --untracked-files=all >/dev/null; then
    echo "test-update: ${phase} 后 operator Git 工作树不可读；拒绝记录成功" >&2
    return 1
  fi
  if ! remote_git_read diff --quiet --no-ext-diff >/dev/null 2>&1; then
    echo "test-update: ${phase} 后 operator Git tracked 工作树不可读或不干净；拒绝记录成功" >&2
    return 1
  fi
  if ! remote_git_read diff --cached --quiet --no-ext-diff >/dev/null 2>&1; then
    echo "test-update: ${phase} 后 operator Git 暂存区不可读或不干净；拒绝记录成功" >&2
    return 1
  fi
}

github_write_preflight() {
  local authenticated_repository
  if [[ "$COMMAND" != apply && "$COMMAND" != rebaseline && "$COMMAND" != promote ]]; then
    return 0
  fi
  command -v gh >/dev/null 2>&1 || {
    echo "test-update: apply/rebaseline/promote 需要 GitHub CLI" >&2
    return 1
  }
  gh auth status --hostname github.com >/dev/null 2>&1 || {
    echo "test-update: GitHub CLI 登录无效；请先通过官方设备登录恢复认证" >&2
    return 1
  }
  authenticated_repository="$(
    gh api "repos/$LOCAL_REPOSITORY" --jq .full_name 2>/dev/null
  )" || {
    echo "test-update: GitHub 仓库只读鉴权预检失败" >&2
    return 1
  }
  authenticated_repository="$(
    printf '%s' "$authenticated_repository" |
      tr '[:upper:]' '[:lower:]'
  )"
  [[ "$authenticated_repository" == "$LOCAL_REPOSITORY" ]] || {
    echo "test-update: GitHub 鉴权上下文与目标仓库不匹配" >&2
    return 1
  }
  echo "test-update: github-auth=verified repository=$LOCAL_REPOSITORY"
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    plan | build | apply | rebaseline | recover-rebaseline-verify | status | promote)
      COMMAND="$1"
      shift
      ;;
  esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      [[ "$COMMAND" == apply ]] || { usage; exit 2; }
      COMMAND="plan"
      shift
      ;;
    --public-snapshot-cutover)
      echo "test-update: public snapshot cutover 已禁用；不得向公开工作区导入私有 Git 对象" >&2
      exit 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REF="$2"
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ "$TARGET" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$ ]] || {
  echo "test-update: SMS_TEST_UPDATE_TARGET 格式无效" >&2
  exit 2
}
[[ "$REMOTE_ROOT" =~ ^/[A-Za-z0-9._/-]+$ && "$REMOTE_ROOT" != *".."* ]] || {
  echo "test-update: SMS_TEST_UPDATE_ROOT 格式无效" >&2
  exit 2
}
[[ "$REMOTE_ROOT" == "$CANONICAL_REMOTE_ROOT" ]] || {
  echo "test-update: SMS_TEST_UPDATE_ROOT 必须为 $CANONICAL_REMOTE_ROOT" >&2
  exit 2
}
[[ "$PORT" =~ ^[0-9]{1,5}$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] || {
  echo "test-update: SMS_TEST_UPDATE_PORT 格式无效" >&2
  exit 2
}
[[ "$REF" =~ ^origin/[A-Za-z0-9._/-]+$ && "$REF" != *".."* ]] || {
  echo "test-update: --ref 必须是安全的 origin 分支" >&2
  exit 2
}
if [[ "$COMMAND" == promote && "$REF" != origin/main ]]; then
  echo "test-update: promote 只允许 origin/main" >&2
  exit 2
fi
if [[ "$COMMAND" == rebaseline && "$REF" != origin/main ]]; then
  echo "test-update: rebaseline 只允许 origin/main" >&2
  exit 2
fi

if [[ "$COMMAND" == recover-rebaseline-verify ]]; then
  [[ "$REF" == origin/main ]] || { usage; exit 2; }
  [[ -n "$VENDOR_ORIGIN" ]] || {
    echo "test-update: recover-rebaseline-verify 要求配置 SMS_VENDOR_LIVE_TEST_ORIGIN" >&2
    exit 2
  }
  remote_bootstrap_sms_compose test-update recover-rebaseline-verify
  exit 0
fi

if [[ "$COMMAND" == status ]]; then
  LOCAL_ORIGIN_URL="$(git -C "$ROOT" remote get-url origin)"
  REMOTE_ORIGIN_URL="$(remote_git_preflight remote get-url origin)"
  if ! LOCAL_REPOSITORY="$(github_repository_identity "$LOCAL_ORIGIN_URL")" ||
    ! REMOTE_REPOSITORY="$(github_repository_identity "$REMOTE_ORIGIN_URL")"; then
    echo "test-update: origin 必须是无凭据的 GitHub 仓库地址" >&2
    exit 1
  fi
  [[ "$LOCAL_REPOSITORY" == "$REMOTE_REPOSITORY" ]] || {
    echo "test-update: 本地与远端 origin 仓库不一致，拒绝读取状态" >&2
    exit 1
  }
  REMOTE_COMMIT="$(remote_git_preflight rev-parse HEAD)"
  remote_git_preflight status --porcelain=v1 --untracked-files=all >/dev/null
  UPDATE_STATUS="$(remote_sms_compose test-update status)"
  VENDOR_STATUS="$(remote_sms_compose vendor-test status)"
  python3 -c 'import json,sys
repo,commit,update_raw,vendor_raw=sys.argv[1:]
assert len(commit)==40 and all(c in "0123456789abcdef" for c in commit)
payload={"repository":repo,"commit":commit,"test_update":json.loads(update_raw),"vendor_test":json.loads(vendor_raw)}
print(json.dumps(payload,separators=(",",":"),sort_keys=True))' \
    "$LOCAL_REPOSITORY" "$REMOTE_COMMIT" "$UPDATE_STATUS" "$VENDOR_STATUS"
  exit 0
fi

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "test-update: 本地工作树必须干净" >&2
  exit 1
fi

git -C "$ROOT" fetch --prune origin
TARGET_COMMIT="$(git -C "$ROOT" rev-parse --verify "$REF^{commit}")"
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 1
LOCAL_ORIGIN_URL="$(git -C "$ROOT" remote get-url origin)"
REMOTE_ORIGIN_URL="$(remote_git_preflight remote get-url origin)"
if ! LOCAL_REPOSITORY="$(github_repository_identity "$LOCAL_ORIGIN_URL")" ||
  ! REMOTE_REPOSITORY="$(github_repository_identity "$REMOTE_ORIGIN_URL")"; then
  echo "test-update: origin 必须是无凭据的 GitHub 仓库地址" >&2
  exit 1
fi
if [[ "$LOCAL_REPOSITORY" != "$REMOTE_REPOSITORY" ]]; then
  echo "test-update: 本地与远端 origin 仓库不一致，拒绝跨仓库更新" >&2
  exit 1
fi
github_write_preflight
REMOTE_COMMIT="$(remote_git_preflight rev-parse HEAD)"
remote_git_preflight status --porcelain=v1 --untracked-files=all >/dev/null
[[ "$REMOTE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || {
  echo "test-update: 远端 commit 无效" >&2
  exit 1
}
if ! git -C "$ROOT" cat-file -e "$REMOTE_COMMIT^{commit}" 2>/dev/null; then
  echo "test-update: 远端基线 commit 不在本地对象库，拒绝把 diff 错误解释为无差异" >&2
  exit 1
fi
echo "test-update: preflight root=$REMOTE_ROOT base=$REMOTE_COMMIT target=$TARGET_COMMIT ref=$REF"

if [[ "$COMMAND" == rebaseline ]] &&
  ! git -C "$ROOT" merge-base --is-ancestor "$REMOTE_COMMIT" "$TARGET_COMMIT"; then
  echo "test-update: rebaseline 只允许服务器基线为目标 main 的祖先" >&2
  exit 1
fi

if [[ "$COMMAND" == promote ]]; then
  BASE_TREE="$(git -C "$ROOT" rev-parse "$REMOTE_COMMIT^{tree}")"
  TARGET_TREE="$(git -C "$ROOT" rev-parse "$TARGET_COMMIT^{tree}")"
  [[ "$BASE_TREE" == "$TARGET_TREE" ]] || {
    echo "test-update: promote 只允许代码树完全相同的提交" >&2
    exit 1
  }
  python3 "$ROOT/scripts/verify_ci_commit.py" \
    --repository "$LOCAL_REPOSITORY" \
    --commit "$TARGET_COMMIT"
  PROMOTE_STATUS="$(
    remote_sms_compose test-update promote "$REF" "$TARGET_COMMIT"
  )"
  verify_operator_git_after_switch promote
  echo "$PROMOTE_STATUS"
  bash "$ROOT/scripts/record_test_deployment.sh" \
    "$LOCAL_REPOSITORY" "$TARGET_COMMIT" "$REF" "Promoted verified test tree to main"
  echo "test-update: state=promoted commit=$TARGET_COMMIT"
  exit 0
fi

BASELINE_STATUS="$(
  remote_sms_compose vendor-test status
)"
MIGRATION_FROM="$(
  python3 -c 'import json,sys; value=json.loads(sys.argv[1])["actual_migration_head"]; assert isinstance(value,str); print(value)' \
    "$BASELINE_STATUS"
)"
ENVIRONMENT_MODE="$(
  python3 -c 'import json,sys
value=json.loads(sys.argv[1]); status=value.get("status")
assert status in {"setup_required","inactive","controlled","blocked"}
if status == "blocked":
 assert value.get("pause_kind") == "critical"
print("pre-live" if status in {"setup_required","inactive"} else "live")' \
    "$BASELINE_STATUS"
)"

DIFF_STATUS=0
git -C "$ROOT" diff --quiet --no-renames \
  "$REMOTE_COMMIT" "$TARGET_COMMIT" || DIFF_STATUS=$?
if [[ "$DIFF_STATUS" == 0 ]]; then
  if [[ "$REMOTE_COMMIT" == "$TARGET_COMMIT" ]]; then
    echo "test-update: 远端已是目标 commit"
    exit 0
  fi
  if [[ "$COMMAND" == plan ]]; then
    echo "test-update: action=promote risk=none base=$REMOTE_COMMIT target=$TARGET_COMMIT ref=$REF"
    exit 0
  fi
  echo "test-update: 代码树相同但 commit 不同；请执行 scripts/test_update.sh promote --ref origin/main" >&2
  exit 1
fi
if [[ "$DIFF_STATUS" -gt 1 ]]; then
  echo "test-update: 无法计算远端基线到目标 commit 的差异" >&2
  exit 1
fi
CHANGED_PATHS=()
while IFS= read -r -d '' path; do
  [[ -n "$path" ]] && CHANGED_PATHS+=("$path")
done < <(
  git -C "$ROOT" diff --name-only --no-renames -z \
    "$REMOTE_COMMIT" "$TARGET_COMMIT"
)
if [[ ${#CHANGED_PATHS[@]} -eq 0 ]]; then
  echo "test-update: 差异存在但路径枚举为空，拒绝继续" >&2
  exit 1
fi

CLASSIFY_ACTION="classify-nul"
if [[ "$COMMAND" == rebaseline ]]; then
  CLASSIFY_ACTION="classify-rebaseline-nul"
fi
SCOPE_JSON="$(
  git -C "$ROOT" diff --name-only --no-renames -z \
    "$REMOTE_COMMIT" "$TARGET_COMMIT" |
    python3 "$ROOT/deploy/scripts/test_update_contract.py" "$CLASSIFY_ACTION"
)" || {
  echo "test-update: 差异分类失败 root=$REMOTE_ROOT base=${REMOTE_COMMIT:0:12} target=${TARGET_COMMIT:0:12} changed=${#CHANGED_PATHS[@]}" >&2
  exit 1
}
RISK="$(
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])["risk"])' \
    "$SCOPE_JSON"
)"
MIGRATION_CHANGED="$(
  python3 -c 'import json,sys; print(int(bool(json.loads(sys.argv[1]).get("migration_changed"))))' \
    "$SCOPE_JSON"
)"
HIGH_RISK_PATHS="$(
  python3 -c 'import json,sys; value=json.loads(sys.argv[1]).get("high_risk_paths",[]); assert isinstance(value,list) and all(isinstance(item,str) for item in value); print(",".join(value) if value else "-")' \
    "$SCOPE_JSON"
)"
API_CHANGED="$(
  python3 -c 'import json,sys; print(int("api" in json.loads(sys.argv[1])["components"]))' \
    "$SCOPE_JSON"
)"
WEB_CHANGED="$(
  python3 -c 'import json,sys; print(int("web" in json.loads(sys.argv[1])["components"]))' \
    "$SCOPE_JSON"
)"
CI_REQUIRED=0
if [[ "$RISK" == high-risk || "$MIGRATION_CHANGED" == 1 ]]; then
  CI_REQUIRED=1
fi
if [[ "$COMMAND" == plan && "$CI_REQUIRED" == 1 ]]; then
  python3 "$ROOT/scripts/verify_ci_commit.py" \
    --repository "$LOCAL_REPOSITORY" \
    --commit "$TARGET_COMMIT" \
    --status-only
elif [[ "$COMMAND" == apply && "$CI_REQUIRED" == 1 ]]; then
  python3 "$ROOT/scripts/verify_ci_commit.py" \
    --repository "$LOCAL_REPOSITORY" \
    --commit "$TARGET_COMMIT"
elif [[ "$COMMAND" == rebaseline ]]; then
  python3 "$ROOT/scripts/verify_ci_commit.py" \
    --repository "$LOCAL_REPOSITORY" \
    --commit "$TARGET_COMMIT" \
    --require-full
fi
if [[ "$RISK" == high-risk &&
      ("$COMMAND" == apply || "$COMMAND" == rebaseline) ]]; then
  REMOTE_SMS_COMPOSE="$BOOTSTRAP_SMS_COMPOSE"
  if ! CAPABILITY_JSON="$(remote_sms_compose test-update capability 2>/dev/null)" ||
    ! python3 -c 'import json,sys
value=json.loads(sys.argv[1])
assert set(value) == {"host_control_snapshot","schema_version","source_commit"}
assert value["host_control_snapshot"] is True
assert value["schema_version"] == 2
assert isinstance(value["source_commit"],str)
assert len(value["source_commit"]) == 40
assert all(character in "0123456789abcdef" for character in value["source_commit"])' \
      "$CAPABILITY_JSON"; then
    echo "test-update: high-risk 更新需要受控 host-control 快照" >&2
    exit 1
  fi
  HOST_SOURCE_COMMIT="$(
    python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_commit"])' \
      "$CAPABILITY_JSON"
  )"
  git -C "$ROOT" cat-file -e "$HOST_SOURCE_COMMIT^{commit}"
  HOST_CONTROL_CHANGED=0
  if git -C "$ROOT" diff --quiet \
    "$HOST_SOURCE_COMMIT" "$TARGET_COMMIT" -- "${HOST_CONTROL_PATHS[@]}"; then
    :
  else
    status=$?
    [[ "$status" == 1 ]] || exit "$status"
    HOST_CONTROL_CHANGED=1
  fi
  if [[ "$HOST_CONTROL_CHANGED" == 1 &&
        "$HOST_SOURCE_COMMIT" != "$TARGET_COMMIT" ]]; then
    echo "test-update: host-control 变更需要先安装同 commit 的不可变快照" >&2
    exit 1
  fi
fi
if [[ "$API_CHANGED" == 1 ]]; then
  UPDATE_KIND="$RISK"
  if [[ "$RISK" != "high-risk" ]]; then
    UPDATE_KIND="backend-safe"
  fi
elif [[ "$WEB_CHANGED" == 1 ]]; then
  UPDATE_KIND="web-only"
else
  echo "test-update: action=none risk=$RISK components=- migration=$MIGRATION_CHANGED high_risk_paths=$HIGH_RISK_PATHS"
  exit 0
fi

WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sms-test-update.XXXXXX")"
WORKTREE="$WORK_ROOT/source"
BUNDLE="$WORK_ROOT/bundle"
cleanup() {
  git -C "$ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf -- "$WORK_ROOT"
}
trap cleanup EXIT
git -C "$ROOT" worktree add --detach "$WORKTREE" "$TARGET_COMMIT"
mkdir -m 0700 "$BUNDLE"
echo "test-update: 目标 commit 已推送；不重复执行组件测试，已按风险执行托管 CI 策略"
MIGRATION_TARGET="$(
  python3 -c 'import ast,pathlib,sys; files=list(pathlib.Path(sys.argv[1]).glob("*.py")); pairs=[]
for path in files:
 tree=ast.parse(path.read_text()); values={}
 for node in tree.body:
  if isinstance(node,(ast.Assign,ast.AnnAssign)):
   targets=node.targets if isinstance(node,ast.Assign) else [node.target]
   for target in targets:
    if isinstance(target,ast.Name) and target.id in {"revision","down_revision"} and isinstance(node.value,ast.Constant): values[target.id]=node.value.value
 if isinstance(values.get("revision"),str): pairs.append((values["revision"],values.get("down_revision")))
parents={parent for _,parent in pairs if isinstance(parent,str)}; heads=sorted(revision for revision,_ in pairs if revision not in parents); assert len(heads)==1; print(heads[0])' \
    "$WORKTREE/backend/migrations/versions"
)"
APP_VERSION="$(tr -d '\n' <"$WORKTREE/VERSION")"
[[ "$APP_VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$ ]] || {
  echo "test-update: 目标版本号无效" >&2
  exit 1
}
if [[ "$MIGRATION_FROM" == "$MIGRATION_TARGET" ]]; then
  MIGRATION_COMPATIBILITY="none"
else
  MIGRATION_COMPATIBILITY="expand"
fi
if [[ "$COMMAND" == rebaseline && "$MIGRATION_COMPATIBILITY" != expand ]]; then
  echo "test-update: rebaseline 要求服务器迁移头真实前移" >&2
  exit 1
fi

PLANNED_COMPONENTS=()
[[ "$API_CHANGED" == 0 ]] || PLANNED_COMPONENTS+=(api)
[[ "$WEB_CHANGED" == 0 ]] || PLANNED_COMPONENTS+=(web)
echo "test-update: action=$COMMAND risk=$RISK components=${PLANNED_COMPONENTS[*]} migration=$MIGRATION_CHANGED compatibility=$MIGRATION_COMPATIBILITY ci_required=$CI_REQUIRED high_risk_paths=$HIGH_RISK_PATHS"
if [[ "$COMMAND" == plan ]]; then
  exit 0
fi

COMPONENTS=()
if [[ "$API_CHANGED" == 1 ]]; then
  API_REF="sms-platform-test-api:$TARGET_COMMIT"
  docker buildx build --platform linux/amd64 --load \
    --build-arg "APP_VERSION=$APP_VERSION" \
    --build-arg "GIT_SHA=$TARGET_COMMIT" \
    --build-arg "SCHEMA_REVISION=$MIGRATION_TARGET" \
    -f "$WORKTREE/backend/Dockerfile" -t "$API_REF" "$WORKTREE"
  API_ID="$(docker image inspect --format '{{.Id}}' "$API_REF")"
  [[ "$API_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
  docker save --output "$BUNDLE/api.tar" "$API_REF"
  COMPONENTS+=(api)
fi
if [[ "$WEB_CHANGED" == 1 ]]; then
  WEB_REF="sms-platform-test-web:$TARGET_COMMIT"
  docker buildx build --platform linux/amd64 --load \
    --build-arg "APP_VERSION=$APP_VERSION" \
    --build-arg "GIT_SHA=$TARGET_COMMIT" \
    --build-arg "SCHEMA_REVISION=$MIGRATION_TARGET" \
    -f "$WORKTREE/frontend/Dockerfile" -t "$WEB_REF" "$WORKTREE"
  WEB_ID="$(docker image inspect --format '{{.Id}}' "$WEB_REF")"
  [[ "$WEB_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
  docker save --output "$BUNDLE/web.tar" "$WEB_REF"
  COMPONENTS+=(web)
fi
chmod 0600 "$BUNDLE"/*.tar

UPDATE_ID="test-$(date -u +%Y%m%dT%H%M%SZ)-${TARGET_COMMIT:0:12}"
REQUEST_OPERATION="apply"
[[ "$COMMAND" != rebaseline ]] || REQUEST_OPERATION="rebaseline"
API_REF_VALUE="${API_REF:-}"
API_ID_VALUE="${API_ID:-}"
API_DIGEST_VALUE=""
WEB_REF_VALUE="${WEB_REF:-}"
WEB_ID_VALUE="${WEB_ID:-}"
WEB_DIGEST_VALUE=""
[[ "$API_CHANGED" == 0 ]] || API_DIGEST_VALUE="$(shasum -a 256 "$BUNDLE/api.tar" | awk '{print $1}')"
[[ "$WEB_CHANGED" == 0 ]] || WEB_DIGEST_VALUE="$(shasum -a 256 "$BUNDLE/web.tar" | awk '{print $1}')"
python3 -c 'import json,sys
out,update_id,base,commit,ref,environment_mode,operation,source,target,compat,*values=sys.argv[1:]
images={}; components=[]
for component,(image_ref,image_id,digest) in zip(("api","web"),(values[:3],values[3:])):
 if image_ref:
  components.append(component); images[component]={"ref":image_ref,"id":image_id,"archive_file":component+".tar","archive_sha256":digest}
payload={"schema_version":1,"update_id":update_id,"base_commit":base,"commit":commit,"source_ref":ref,"environment_mode":environment_mode,"operation":operation,"components":components,"images":images,"migration":{"from":source,"target":target,"compatibility":compat}}
path=__import__("pathlib").Path(out); path.write_text(json.dumps(payload,separators=(",",":"),sort_keys=True)+"\n")' \
  "$BUNDLE/request.json" "$UPDATE_ID" "$REMOTE_COMMIT" "$TARGET_COMMIT" "$REF" \
  "$ENVIRONMENT_MODE" "$REQUEST_OPERATION" \
  "$MIGRATION_FROM" "$MIGRATION_TARGET" "$MIGRATION_COMPATIBILITY" \
  "$API_REF_VALUE" "$API_ID_VALUE" "$API_DIGEST_VALUE" \
  "$WEB_REF_VALUE" "$WEB_ID_VALUE" "$WEB_DIGEST_VALUE"
chmod 0600 "$BUNDLE/request.json"

if [[ "$COMMAND" == build ]]; then
  echo "test-update: state=built risk=$RISK components=${COMPONENTS[*]} commit=$TARGET_COMMIT"
  exit 0
fi

REMOTE_INCOMING="/var/lib/sms-platform/test-updates/incoming"
ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "$TARGET" sudo install -d -m 0700 -- "/var/lib/sms-platform/test-updates"
ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "$TARGET" sudo install -d -m 0700 -- "$REMOTE_INCOMING"

REMOTE_STAGING="/tmp/sms-test-update-upload.$TARGET_COMMIT"
ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "$TARGET" mkdir -m 0700 -p -- "$REMOTE_STAGING"
REMOTE_STAGING_META="$(
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" "env LC_ALL=C stat -c '%u|%a|%F' -- '$REMOTE_STAGING'"
)"
REMOTE_USER_UID="$(
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" id -u
)"
[[ "$REMOTE_USER_UID" =~ ^[0-9]+$ &&
    "$REMOTE_STAGING_META" == "$REMOTE_USER_UID|700|directory" ]] || {
  echo "test-update: 远端上传暂存目录不安全" >&2
  exit 1
}
REMOTE_UPLOAD_ID="${TARGET_COMMIT:0:12}"
RSYNC_SSH="ssh -p $PORT -o BatchMode=yes -o StrictHostKeyChecking=yes"

UPLOAD_ARTIFACTS=()
[[ "$API_CHANGED" == 0 ]] || UPLOAD_ARTIFACTS+=("$BUNDLE/api.tar")
[[ "$WEB_CHANGED" == 0 ]] || UPLOAD_ARTIFACTS+=("$BUNDLE/web.tar")
# request.json 最后发布，确保固定 incoming 永远不会先暴露不完整请求。
UPLOAD_ARTIFACTS+=("$BUNDLE/request.json")

for artifact in "${UPLOAD_ARTIFACTS[@]}"; do
  name="$(basename "$artifact")"
  EXPECTED_SHA256="$(shasum -a 256 "$artifact" | awk '{print $1}')"
  REMOTE_PART="$REMOTE_STAGING/$name.part"
  UPLOADED=0
  for attempt in 1 2 3; do
    if rsync \
      "--partial" \
      "--inplace" \
      "--chmod=Fu=rw,Fgo=" \
      -e "$RSYNC_SSH" \
      "$artifact" "$TARGET:$REMOTE_PART"; then
      UPLOADED=1
      break
    fi
    echo "test-update: 上传中断，重试 $attempt/3（保留固定 .part）" >&2
  done
  if [[ "$UPLOADED" != 1 ]]; then
    echo "test-update: 上传失败；远端续传文件保留在 $REMOTE_PART" >&2
    exit 1
  fi

  REMOTE_SHA256_OUTPUT="$(
    ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
      "$TARGET" sha256sum -- "$REMOTE_PART"
  )"
  REMOTE_SHA256="$(
    python3 -c 'import re,sys
lines=sys.argv[1].splitlines()
assert len(lines)==1
fields=lines[0].split()
assert len(fields)==2 and fields[1]==sys.argv[2]
assert re.fullmatch(r"[0-9a-f]{64}",fields[0])
print(fields[0])' "$REMOTE_SHA256_OUTPUT" "$REMOTE_PART"
  )" || {
    echo "test-update: 远端上传摘要输出无效；保留 .part" >&2
    exit 1
  }
  [[ "$REMOTE_SHA256" == "$EXPECTED_SHA256" ]] || {
    echo "test-update: 远端上传摘要不匹配；保留 .part" >&2
    exit 1
  }

  REMOTE_PUBLISH_TEMP="$REMOTE_INCOMING/.${name}.${EXPECTED_SHA256}.${REMOTE_UPLOAD_ID}.tmp"
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" sudo install -o root -g root -m 0600 -- "$REMOTE_PART" "$REMOTE_PUBLISH_TEMP"
  PUBLISHED_SHA256_OUTPUT="$(
    ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
      "$TARGET" sudo sha256sum -- "$REMOTE_PUBLISH_TEMP"
  )"
  PUBLISHED_SHA256="$(
    python3 -c 'import re,sys
lines=sys.argv[1].splitlines()
assert len(lines)==1
fields=lines[0].split()
assert len(fields)==2 and fields[1]==sys.argv[2]
assert re.fullmatch(r"[0-9a-f]{64}",fields[0])
print(fields[0])' "$PUBLISHED_SHA256_OUTPUT" "$REMOTE_PUBLISH_TEMP"
  )" || {
    echo "test-update: root 发布暂存摘要输出无效；保留上传 .part" >&2
    exit 1
  }
  [[ "$PUBLISHED_SHA256" == "$EXPECTED_SHA256" ]] || {
    echo "test-update: root 发布暂存摘要不匹配；保留上传 .part" >&2
    exit 1
  }
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" sudo mv -- "$REMOTE_PUBLISH_TEMP" "$REMOTE_INCOMING/$name"
done

for artifact in "${UPLOAD_ARTIFACTS[@]}"; do
  name="$(basename "$artifact")"
  ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$TARGET" rm -- "$REMOTE_STAGING/$name.part"
done
ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "$TARGET" rmdir -- "$REMOTE_STAGING"

# 固定远端阶段: sms-compose test-update prepare
remote_sms_compose test-update prepare
# 固定远端阶段: sms-compose test-update apply
remote_sms_compose test-update apply
# 固定远端阶段: sms-compose test-update verify
remote_sms_compose test-update verify
# verify 后再次以日常更新用户核对 origin、HEAD 和工作树读路径；root 控制面通过不代表 operator 可用。
verify_operator_git_after_switch "$COMMAND"
# 固定远端阶段: sms-compose test-update status
FINAL_STATUS="$(remote_sms_compose test-update status)"
if ! printf '%s' "$FINAL_STATUS" |
  python3 "$ROOT/deploy/scripts/test_update_contract.py" verify-status \
    "$UPDATE_ID" "$TARGET_COMMIT" "$MIGRATION_TARGET" >/dev/null; then
  echo "test-update: 远端终态未达到目标 verified" >&2
  exit 1
fi
bash "$ROOT/scripts/record_test_deployment.sh" \
  "$LOCAL_REPOSITORY" "$TARGET_COMMIT" "$REF" "Verified $COMMAND on test server"
echo "test-update: state=verified commit=$TARGET_COMMIT"
