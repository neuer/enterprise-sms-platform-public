#!/usr/bin/env bash
# 共享测试环境“铲掉重建”：销毁全部测试数据后按精确 commit 重建纯 Mock 全栈。
# 测试环境不保存正式厂商凭据，也不保留任何需要迁移的数据；生产发布不走本入口。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CONFIG_FILE="$ROOT/.env.test-update"
REMOTE_ROOT="/opt/sms-platform"
LIVE_MARKER="/etc/sms-platform/test-environment"
REF="origin/main"
DRY_RUN=0

usage() {
  echo "usage: scripts/test_rebuild.sh [--ref origin/BRANCH] [--dry-run]" >&2
}

fail() {
  echo "test-rebuild: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REF="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done
[[ "$REF" =~ ^origin/[A-Za-z0-9._/-]+$ && "$REF" != *".."* ]] || {
  echo "test-rebuild: --ref 必须是安全的 origin 分支" >&2
  exit 2
}

# 只读取 SSH 目标与端口；文件不得包含密码或私钥。
CONFIG_TARGET=""
CONFIG_PORT=""
if [[ -e "$CONFIG_FILE" ]]; then
  [[ -f "$CONFIG_FILE" && ! -L "$CONFIG_FILE" && -O "$CONFIG_FILE" ]] ||
    fail ".env.test-update 必须是当前用户拥有的普通文件"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "" | \#*) ;;
      SMS_TEST_UPDATE_TARGET=*) CONFIG_TARGET="${line#*=}" ;;
      SMS_TEST_UPDATE_PORT=*) CONFIG_PORT="${line#*=}" ;;
      # 历史键：测试环境已不再做真实联调，保留只为兼容既有本地文件。
      SMS_VENDOR_LIVE_TEST_ORIGIN=*) ;;
      *) fail ".env.test-update 只允许 SMS_TEST_UPDATE_TARGET 与 SMS_TEST_UPDATE_PORT" ;;
    esac
  done <"$CONFIG_FILE"
fi
TARGET="${SMS_TEST_UPDATE_TARGET:-$CONFIG_TARGET}"
PORT="${SMS_TEST_UPDATE_PORT:-${CONFIG_PORT:-22}}"
WEB_PORT="${SMS_TEST_WEB_PORT:-18080}"
[[ "$TARGET" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$ ]] ||
  fail "缺少或无效的 SMS_TEST_UPDATE_TARGET（user@host）"
[[ "$PORT" =~ ^[0-9]{1,5}$ && "$WEB_PORT" =~ ^[0-9]{1,5}$ ]] || fail "端口无效"

git -C "$ROOT" fetch --prune origin
COMMIT="$(git -C "$ROOT" rev-parse --verify "$REF^{commit}")"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "无法解析 $REF"
REPOSITORY="$(
  git -C "$ROOT" remote get-url origin |
    sed -E 's#^(git@github\.com:|https://github\.com/|ssh://git@github\.com/)##; s#\.git$##'
)"
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
  fail "origin 必须是无凭据的 GitHub 仓库地址"

# 只部署 ci-gate 已成功的精确 commit。
python3 "$ROOT/scripts/verify_ci_commit.py" --repository "$REPOSITORY" --commit "$COMMIT"

echo "test-rebuild: target=$TARGET root=$REMOTE_ROOT ref=$REF commit=$COMMIT"
if [[ "$DRY_RUN" == 1 ]]; then
  echo "test-rebuild: dry-run，未连接服务器"
  exit 0
fi

# 远端固定步骤；参数只有已校验的路径、commit 与端口。
ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes "$TARGET" \
  sudo /bin/bash -s -- "$REMOTE_ROOT" "$COMMIT" "$WEB_PORT" "$LIVE_MARKER" <<'REMOTE'
set -euo pipefail
root="$1"; commit="$2"; web_port="$3"; live_marker="$4"
if [[ -e "$live_marker" ]]; then
  echo "test-rebuild: 服务器仍处于真实联调状态；先在页面切回 Mock 并清除正式凭据" >&2
  exit 1
fi
cd "$root"
git fetch --prune origin
git checkout --quiet --detach "$commit"
[[ "$(git rev-parse HEAD)" == "$commit" ]]
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
  echo "test-rebuild: 服务器 checkout 存在已跟踪文件改动" >&2
  exit 1
}
# local_test.sh 自行拒绝非纯 Mock 的 .env，并执行 down -v、重建、迁移、切换与 seed。
WEB_PORT="$web_port" SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  bash scripts/local_test.sh reset
REMOTE

bash "$ROOT/scripts/record_test_deployment.sh" \
  "$REPOSITORY" "$COMMIT" "$REF" "Rebuilt test server from scratch (Mock only)"
echo "test-rebuild: state=rebuilt commit=$COMMIT"
