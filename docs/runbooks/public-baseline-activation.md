# 无共同历史测试服务器的一次性公开基线激活

## 1. 适用范围与停止条件

本手册只处理一次：旧测试服务器 `/opt/sms-platform` 的 Git 历史不在规范公开仓库对象库，
但数据库 schema 已是 `0039_manual_job_outbox`，需要在保留全部运行数据的前提下，把
应用根切换到公开仓库 `neuer/enterprise-sms-platform-public` 的精确 `main`。完成后退役
本入口，日常更新恢复 `scripts/test_update.sh apply --ref origin/main`。

以下任一条件不满足，立即停止，不得用 raw Git、手工 Compose、旧
`--public-snapshot-cutover` 或数据库初始化绕过：

- 变更单已明确目标 commit、维护窗口、操作者、验收人与回退责任人；
- 目标是公开仓库精确 `main`，且该 commit 的 GitHub Actions `ci-gate` 已成功；
- 隔离 checkout 只含公开 `main` 的完整对象，不含 shallow/promisor/alternate、额外 ref、
  不可达对象或私有 URL；
- 服务器工作树干净，没有进行中的 release/test-update/vendor rotation，`submitting`、
  `retrying`、`uncertain` 均为零；
- 服务器与目标 migration head 都是 `0039_manual_job_outbox`；本流程无迁移；
- PostgreSQL、全部 Docker volume、三域 Redis、七职责数据库角色和真实联调安全投影
  均可只读观测；
- root-owned host-control 安装器及固定 cloudflared 二进制可用。

公开 bundle、镜像和 JSON 不得包含私有 Git 对象、手机号、HMAC、短信正文、Key 值或其
长度/前缀/摘要。正式厂商 Key、加密测试号码和真实联调模式不是本流程输入，激活前后不得
安装、轮换、删除、重录、暂停或恢复它们。

## 2. 固定边界

| 项目 | 固定值或语义 |
|---|---|
| 规范仓库 | `https://github.com/neuer/enterprise-sms-platform-public.git` |
| 目标 ref | `refs/heads/main` |
| 应用根 | `/opt/sms-platform` |
| 远端 incoming | `/var/lib/sms-platform/test-updates/incoming`，root `0700` |
| manifest/request | `baseline-manifest.json` / `request.json`，root `0600` |
| host wrapper | `/usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap` |
| 迁移 | from=target=`0039_manual_job_outbox`，compatibility=`none` |
| 组件/风险 | `api` + `web`，固定 `high-risk` |
| 主机身份 | root UID=`0`；固定服务器 operator UID/GID=`1000:1000` |
| active root profile | `/opt/sms-platform` 为 `0:1000 2770`；`backend`、`deploy` 为 `1000:1000 2770` |
| Git metadata profile | `/opt/sms-platform/.git` 及其递归内容由 root 拥有、group=`1000`；目录至少对 group 可读/遍历，文件至少对 group 可读；operator 必须能完成 `remote get-url`、`rev-parse`、`status` |
| 持久项 | 只把旧 `.env`、`deploy/secrets`、`backend/.venv` rename 到目标根；secrets 目录固定 `0:1000 0700`，目录内权威文件固定 `0:0 0600` |
| 根外事实 | PostgreSQL、Docker volume、Redis、角色、vendor-test 运行态原位保留 |

`prepare/apply/verify/finalize/cleanup` 共用 lifecycle lock；`status` 是无锁只读入口。
根切换使用同文件系统目录 exchange。旧 root 始终先留在 activation 专属 recovery 路径；
finalize 只把 journal 确认为 `verified`，不删除 recovery。只有 verified 后的表面验收
全部通过，才单独运行 cleanup 删除旧 root 和三个大传输产物。

执行前必须以 `id -u smsops`、`id -g smsops`（或服务器约定 operator 账号）确认两者都
精确为 `1000`；任何漂移都停止，不得把实际 UID/GID 动态代入以放宽合同。

## 3. 维护窗口前的只读基线

在服务器的受控 root 会话中执行，只记录无 PII 结果：

```bash
cd /opt/sms-platform
git status --porcelain
git rev-parse --verify HEAD^{commit}
git rev-parse --verify HEAD^{tree}

sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  /usr/local/sbin/sms-compose ps --status running

sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  /usr/local/sbin/sms-compose exec -T postgres \
  psql -U sms_owner -d sms -Atqc \
  'SELECT version_num FROM alembic_version'

sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  /usr/local/sbin/sms-compose vendor-test status

docker volume ls --format '{{.Name}}' | LC_ALL=C sort
```

第一条必须无输出，migration 必须精确为 `0039_manual_job_outbox`。保存 base commit/tree、
运行服务、volume 名称集合、数据库职责角色集合，以及 vendor-test 安全投影中的
`credential_configured`、`active_recipient_count`、mode/pause；不得读取或记录凭据文件、
generation、号码、HMAC 或密文。另确认 operator UID/GID 精确为 `1000:1000`，active root
为 `0:1000 2770`，其 `backend`、`deploy` 为 `1000:1000 2770`；只检查以下持久路径存在、
owner/mode 合法，不计算内容摘要：

```bash
id -u smsops
id -g smsops
sudo stat -c '%u:%g %a %F %n' \
  /opt/sms-platform \
  /opt/sms-platform/backend \
  /opt/sms-platform/deploy
sudo stat -c '%U:%G %a %F %n' \
  /opt/sms-platform/.env \
  /opt/sms-platform/deploy/secrets \
  /opt/sms-platform/backend/.venv
sudo find /opt/sms-platform/deploy/secrets \
  -mindepth 1 -maxdepth 1 \
  -printf '%U:%G %m %y %n %p\n'
```

secrets 目录内每项必须是 `0:0 600 f 1` 的非空普通文件；该检查只显示文件名和元数据，
不得读取或输出值、长度、摘要或哈希。development 测试环境允许额外的
`dev-apikeys.txt`，但它同样必须满足上述 root-only 元数据合同；production 禁止该文件。

数据库角色权限和三域 Redis ACL 继续使用 `deploy/database-roles.md` 与
`scripts/verify_redis_domains.sh` 的既有只读验收；不得为本次激活创建新角色、密码或
Redis 实例。

## 4. 在隔离公开 checkout 生成 bundle

从当前公开仓库根执行。隔离 checkout 和 artifact 目录必须在当前仓库之外；不要复用个人
开发 clone，不要使用 `--depth`、partial clone 或 alternates：

```bash
PUBLIC_REPO="$(git rev-parse --show-toplevel)"
git -C "$PUBLIC_REPO" fetch --prune --no-tags origin \
  refs/heads/main:refs/remotes/origin/main
TARGET_COMMIT="$(git -C "$PUBLIC_REPO" rev-parse \
  refs/remotes/origin/main^{commit})"
test "$(git -C "$PUBLIC_REPO" rev-parse HEAD^{commit})" = "$TARGET_COMMIT"
test -z "$(git -C "$PUBLIC_REPO" status --porcelain)"

BASE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sms-public-baseline.XXXXXX")"
chmod 0700 "$BASE_DIR"
WORKSPACE="$BASE_DIR/public-main"
ARTIFACTS="$BASE_DIR/artifacts"
PREPARE_SUMMARY="$BASE_DIR/prepare-summary.json"
FINALIZE_SUMMARY="$BASE_DIR/finalize-summary.json"

git clone --no-tags --single-branch --branch main \
  https://github.com/neuer/enterprise-sms-platform-public.git \
  "$WORKSPACE"
test "$(git -C "$WORKSPACE" rev-parse HEAD^{commit})" = "$TARGET_COMMIT"

python3 "$PUBLIC_REPO/scripts/public_baseline_activate.py" prepare \
  --workspace "$WORKSPACE" \
  --artifacts "$ARTIFACTS" \
  --repository neuer/enterprise-sms-platform-public \
  --commit "$TARGET_COMMIT" |
  tee "$PREPARE_SUMMARY"
```

`prepare` 会再次从 GitHub 验证公开仓库身份与目标 commit 的精确 CI，检查 refs、完整对象
集合和干净状态，生成：

- `public-baseline.bundle`：只含完整 `refs/heads/main`；
- `public-object-inventory.json`：commit/tree、对象数和对象集合摘要；
- stdout 摘要：`update_id`、target commit/tree、bundle SHA-256、对象数与对象摘要。

artifact 目录必须是当前用户拥有的真实 `0700` 目录。driver 会拒绝 external gitdir、
gitfile、linked worktree、alternate、shallow/promisor、额外 ref、不可达对象、ignored
文件、symlink/hardlink、Git filter/LFS、行尾或 mode 漂移；CI 不成功或对象集合不闭合也
会失败关闭。

## 5. 构建 API/Web amd64 镜像

构建只能由同一 driver 的 `build` 阶段执行；不得从可变 checkout 手工运行
`docker build/save`，也不得手工抄录 image ID：

```bash
BUILD_SUMMARY="$BASE_DIR/build-summary.json"

python3 "$PUBLIC_REPO/scripts/public_baseline_activate.py" build \
  --workspace "$WORKSPACE" \
  --artifacts "$ARTIFACTS" \
  --repository neuer/enterprise-sms-platform-public \
  --commit "$TARGET_COMMIT" |
  tee "$BUILD_SUMMARY"
```

driver 先把已验证 bundle 复制到私有临时目录，从其中的原始 Git blob 逐文件物化规范化
build context，并在每次构建前后复验路径、Git mode、大小和内容摘要；它不执行 checkout、
clean/smudge 或 LFS hydration。随后以固定 argv 构建 API/Web `linux/amd64` 镜像，自动
inspect ID、architecture、version/revision/schema 三个 label，再生成并解析验证
docker-save archive。成功生成：

- `api.tar`、`web.tar`：当前用户 `0600` 普通单链接文件；
- `built-images.json`：本地冻结的 context 与两张镜像身份，仅供 finalize 复验，不上传；
- stdout `state=images-ready`，包含两张镜像的 ID 与 archive SHA-256。

image ID 必须是 `sha256:` 加 64 位小写十六进制，revision 必须精确为
`$TARGET_COMMIT`，schema 必须是 `0039_manual_job_outbox`。不得给 build 增加 secret、
额外 context、私有 registry 或其他参数。

## 6. 生成最终 manifest 和标准 request

把第 3 节服务器输出作为不透明 base 身份；不要把旧服务器私有 origin、ref、提交内容或
对象复制到本地公开工作区：

```bash
BASE_COMMIT='<服务器当前40位commit>'
BASE_TREE='<服务器当前40位tree>'
MIGRATION_HEAD=0039_manual_job_outbox
ENVIRONMENT_MODE=live  # 仅纯 Mock/未激活环境才使用 pre-live

python3 "$PUBLIC_REPO/scripts/public_baseline_activate.py" finalize \
  --workspace "$WORKSPACE" \
  --artifacts "$ARTIFACTS" \
  --repository neuer/enterprise-sms-platform-public \
  --commit "$TARGET_COMMIT" \
  --base-commit "$BASE_COMMIT" \
  --base-tree "$BASE_TREE" \
  --migration-head "$MIGRATION_HEAD" \
  --environment-mode "$ENVIRONMENT_MODE" |
  tee "$FINALIZE_SUMMARY"

(cd "$ARTIFACTS" && sha256sum \
  public-baseline.bundle \
  api.tar \
  web.tar \
  baseline-manifest.json \
  request.json) > "$BASE_DIR/upload-sha256.txt"
chmod 0600 "$BASE_DIR/upload-sha256.txt"
```

`finalize` 不接受手工 image ID。它重新验证 workspace、GitHub CI、bundle、对象摘要、
`built-images.json`、规范 context 身份和两个镜像归档；manifest/request 从同一份冻结
镜像身份一次生成并逐字段交叉绑定。它先新建 `baseline-manifest.json`，最后新建标准
`request.json`。两者 activation/update ID 必须相同，格式为
`test-YYYYMMDDTHHMMSSZ-<target前12位>`。manifest 绑定 base/target commit+tree、bundle、
两个镜像的版本/revision/schema label 和无迁移合同；request 绑定既有 test-update parser。

## 7. 摘要校验上传与 target host-control 安装

传输不是 driver 的能力。变更单必须把下述 `<受控上传>`/`<受控远程执行>` 展开为已有、
审核过的固定 SSH/rsync argv；不得使用 `eval`、agent forwarding、私有 Git remote 或把
主机地址/账号写进仓库。上传顺序固定：

1. `public-baseline.bundle`
2. `api.tar`
3. `web.tar`
4. `baseline-manifest.json`
5. `request.json`（最后发布）

`public-object-inventory.json`、`built-images.json` 和三个本地 stdout summary 不上传。
`request.json` 是唯一发布标记；其余四个固定服务端产物没有全部安全落盘前，服务器不得
看到该文件。

先把 bundle 和 `upload-sha256.txt` 传到服务器 root `0700` 临时上传目录。服务器复算
bundle SHA-256，与本地摘要和 manifest 一致后，原子发布为：

```text
/var/lib/sms-platform/test-updates/incoming/public-baseline.bundle
```

文件必须是 root:root `0600` 普通单链接文件，incoming 及祖先不得是符号链接。此时先用
**公开 bundle** 在旧对象库创建临时 ref；这不是把私有对象带入公开工作区：

```bash
TARGET_COMMIT='<第4节目标commit>'
TARGET_TREE='<第4节目标tree>'
INCOMING=/var/lib/sms-platform/test-updates/incoming
TEMP_REF="refs/public-baseline/$TARGET_COMMIT"

git -C /opt/sms-platform bundle verify \
  "$INCOMING/public-baseline.bundle"
git -C /opt/sms-platform fetch --no-tags \
  "$INCOMING/public-baseline.bundle" \
  "refs/heads/main:$TEMP_REF"
test "$(git -C /opt/sms-platform rev-parse \
  "$TEMP_REF^{commit}")" = "$TARGET_COMMIT"
test "$(git -C /opt/sms-platform rev-parse \
  "$TEMP_REF^{tree}")" = "$TARGET_TREE"
```

随后把 target host-control 从该 ref 归档到 commit 专属 root staging。资产列表与 mode
必须逐字采用 `deploy/README.md`「一次性主机安装」：其中包含
`public_baseline_activation.py` 和 `public_baseline_manager.py`；把该节 fetch 命令替换为
上面的 bundle/ref 验证，其余 `git archive`、逐项 `chmod 0644/0755`、
`install_test_secure_access.py --source-commit "$TARGET_COMMIT"`、`systemd-analyze verify`
和 `verify-assets` 原样执行。cloudflared 仍使用该节锁定的版本与 SHA-256，不从服务器网络
临时下载。

安装成功后，先确认 root-owned asset manifest 返回的 `source_commit` 精确等于
`$TARGET_COMMIT`，再以 compare-and-delete 删除临时 ref：

```bash
sudo /usr/bin/env \
  SMS_SECURE_ACCESS_INTERNAL=1 \
  SMS_SECRETS_MODE=development \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH=/usr/local/libexec/sms-platform/test-secure-access \
  /usr/bin/python3 \
  /usr/local/libexec/sms-platform/test-secure-access/test_secure_access_manager.py \
  verify-assets

git -C /opt/sms-platform update-ref -d \
  "$TEMP_REF" "$TARGET_COMMIT"
test -z "$(git -C /opt/sms-platform for-each-ref \
  --format='%(refname)' "$TEMP_REF")"
```

不要运行 `git gc`：公开对象可以随旧 root 一起进入 recovery，不得为了清理对象而改写
恢复根。

然后上传并逐一复算 API/Web 归档与 manifest 摘要，先以 root `0600` `.part` 文件落盘，
摘要一致后同目录 rename 到固定名。所有四项完成后，最后才以相同步骤发布
`request.json`。禁止覆盖另一个 activation ID 的 incoming 或状态目录；发现残留先停止并
按原变更单对账。

## 8. 六个固定服务端命令

只使用已安装的 host bootstrap wrapper。普通
`/usr/local/sbin/sms-compose`/checkout manager、production mode、额外参数和直接
`__locked` 都会被拒绝：

```bash
BASELINE_COMPOSE=(
  sudo /usr/bin/env
  SMS_PLATFORM_ROOT=/opt/sms-platform
  SMS_SECRETS_MODE=development
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials
  /usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap
)

"${BASELINE_COMPOSE[@]}" test-update baseline-prepare
"${BASELINE_COMPOSE[@]}" test-update baseline-apply
"${BASELINE_COMPOSE[@]}" test-update baseline-verify
"${BASELINE_COMPOSE[@]}" test-update baseline-status
"${BASELINE_COMPOSE[@]}" test-update baseline-finalize
"${BASELINE_COMPOSE[@]}" test-update baseline-status
```

此时先执行第 9 节表面验收。只有全部通过，才执行第 11 节的：

```bash
"${BASELINE_COMPOSE[@]}" test-update baseline-cleanup
"${BASELINE_COMPOSE[@]}" test-update baseline-status
```

- `baseline-prepare`：验证 manifest/request、公开 bundle、独立 target staging、
  target-bound host source、API/Web archive；load 后验证 image ID、amd64 与三个 OCI
  label；建立标准 store，以 high-risk/no-migration 暂停两条 lane，并拒绝
  submitting/retrying/uncertain。
- `baseline-apply`：在锁内迁移三项 allowlist，原子 exchange 应用根；原子更新并验证
  `vendor-control-agent` unit；保存旧镜像身份并重建 API/Web 与固定后端服务。
- `baseline-verify`：复用既有环境模式、账本守恒、pause 所有权、live GetBalance、服务与
  migration head 验收；通过后 test-update 状态成为 `verified` 并释放本次 update pause。
- `baseline-status`：无锁只读，只返回 activation/state、实际和目标 commit/tree、
  migration head 以及 `operator_git_access`；不得包含 secret、号码、HMAC 或正文。该字段
  必须为 `true`，否则不得 finalize/cleanup。
- `baseline-finalize`：只接受已 verified 的标准 store；再次验证 source/unit/images/
  服务后把 core journal 确认为 verified，保留旧 recovery root 与旧镜像回退标签。
- `baseline-cleanup`：只接受已 verified/finalized 且已完成表面验收的本次 activation；
  以固定 tombstone 可重入删除旧 recovery root，严格删除并复核旧镜像回退标签，再删除
  incoming 中精确属于本次的
  `public-baseline.bundle`、`api.tar`、`web.tar`。它保留
  `baseline-manifest.json`、`request.json`、标准 test-update store 和 core journal。

不要并行运行这些命令；不要把 status 的无锁语义扩大到其他五项。cleanup 不是 finalize
的别名，也不是失败时的回退入口。

## 9. verified 后逐项验收

`baseline-status` 必须同时显示 `state=verified`、`operator_git_access=true`、
actual=target commit/tree、`actual_migration_head=0039_manual_job_outbox`。随后以日常
operator 身份执行一次 `scripts/test_update.sh status`；它必须能读取同一 `/opt/sms-platform`
的 `origin`、HEAD 和工作树状态。任一步失败都停止，不得 finalize/cleanup。然后逐项确认：

1. `/opt/sms-platform` 干净，HEAD、tree、`origin` 与 `origin/main` 精确绑定规范公开
   仓库 target；
2. API/Web 当前 image ID 等于 manifest；architecture=amd64，version/revision/schema
   三个 label 与 manifest 一致；
3. api、web、worker-realtime、worker-report、worker-bulk、worker-callback、outbox-dispatcher、beat
   均 running/healthy，mock-vendor 未被引入；
4. `vendor-control-agent.service` 的已安装字节等于 target unit，service active，重启计数
   不持续增长；
5. Alembic head 仍是 `0039_manual_job_outbox`，没有执行 migrate、没有新 checkpoint；
6. PostgreSQL 数据库、切换前全部 Docker volume、三域 Redis 及 ACL、七职责数据库角色
   与权限矩阵逐项不变；
7. `.env`、`deploy/secrets`、`backend/.venv` 仍存在且 owner/mode 合法；它们是 rename
   保留，不是复制后重建；
8. vendor-test 的 credential-configured 布尔值、active recipient 数、mode、manual/
   critical/daily pause 与切换前业务事实一致；正式 Key 与号码没有被读取、回显、轮换或
   重录；
9. HTTP/HTTPS 登录、配置页、只读列表和一项不产生真实短信的目标表面验收通过。真实 UAT
   仍按 `controlled-real-vendor-test.md` 独立授权，不属于本次激活。

变更证据只保留 activation ID、commit/tree、CI run、bundle/object 摘要、image
ID/archive SHA、无 PII 状态、服务健康、migration head 和上述集合比对结论。

## 10. 失败与回退

- prepare 失败：不得 apply；保留 incoming、标准 store、core journal 和 update pause
  供对账。若 manager 已进入 fail closed，不得手工删 Redis pause。
- prepare 在切换前因可修复的目标合同失败时，只能把修复合入新的精确 `main`，重新生成
  全套制品并使用新的 activation ID；不得复用 terminal `blocked` ID。旧 store 与两条
  pause 必须保留，由新 prepare 内建的 blocked-predecessor 校验和单条 Redis Lua CAS
  原子接管，禁止手工删除、改写或短暂解暂停。
- root/unit/image/service 在 apply 失败：manager 依次恢复旧 root、旧
  `vendor-control-agent` unit、旧 API/Web 镜像和服务；恢复验证成功后状态为
  `rolled_back`，命令仍失败退出。
- verify 失败：走同一无迁移回退；绝不 checkout 私有 target、回退 schema、切回 Mock、
  清库、删卷或重新生成 secrets。
- 任一回退步骤失败：状态必须是 `blocked`，两条发送 lane 保持 fail closed；停止操作，
  保留 recovery/staged root、journal、incoming 与镜像，不得尝试 finalize 或手工交换
  目录。
- 已 `verified`、cleanup 前发现业务验收问题：不得 cleanup。立即停止发送并另立恢复
  变更；recovery root 仍保留，但没有公开的任意 rollback 命令，必须按 core journal 和
  固定 host-control 单独评审。
- cleanup 成功后才发现问题：旧 root 已按授权删除，不得假装仍有本地 recovery；保持
  fail closed，基于保留的 manifest/request/store/journal 与正常公开发布材料另立恢复
  变更，仍不得回退 schema、清库或删卷。

每次失败先运行一次 `baseline-status` 并记录无 PII 结果；不要反复 prepare/apply 覆盖现场。

## 11. verified 与表面验收后的 cleanup

只有第 9 节全部通过、`baseline-finalize` 成功且第二次 status 仍为 `verified`，才把允许的
无 PII 摘要附到变更单并执行：

```bash
"${BASELINE_COMPOSE[@]}" test-update baseline-cleanup
"${BASELINE_COMPOSE[@]}" test-update baseline-status
```

`baseline-cleanup` 是本流程唯一允许的旧根/大产物删除入口。它先把 recovery root 原子改名
为 activation 专属 cleanup tombstone，以已记录的 device/inode 和 fd-safe 遍历可重入
删除；中断后只允许重跑同一命令。随后严格删除并复核本次两个旧镜像回退标签，再只删除
incoming 中以下三个 root `0600` 普通单链接文件：

```text
public-baseline.bundle
api.tar
web.tar
```

cleanup 成功后必须确认 recovery/tombstone 和上述三文件均不存在，当前公开 root、服务、
volume 与运行态仍通过第 9 节的只读检查。标准 test-update 状态仍保留 verified 证据；
core journal 记录 cleaned 终态。

以下对象明确**保留**：

- incoming 的 `baseline-manifest.json` 与 `request.json`；
- 标准 test-update store 与 activation core journal；
- `.env`、`deploy/secrets`、`backend/.venv`；
- PostgreSQL、Docker volume、三域 Redis、数据库角色和运行态目录；
- 正式 Key、credential generation、加密测试号码、账本、audit；
- 当前 target 镜像、运行容器和 root-owned host-control。

不得手工递归删除 incoming、`/var/lib/sms-platform`、`/opt/sms-platform`、recovery 或
tombstone；cleanup 失败时保留现场并停止。上传临时目录中的 `.part`/摘要只有在逐项确认
属于本次 activation 后才可精确删除。本地 `BASE_DIR` 可移入受控废纸篓，或在人工确认它
确为本次 `mktemp` 目录后清除；其中 `built-images.json` 只存在本地，不得上传或加入当前
Git 仓库。安装 target host-control 时创建的临时 ref 已在第 7 节 compare-and-delete，
不得运行 `git gc`。

## 12. 恢复日常流程

在新的公开基线上先做只读诊断，再恢复维护入口：

```bash
scripts/test_update.sh status
git fetch origin
scripts/test_update.sh plan --ref origin/main
```

下一次确有共享环境验收需求时才运行：

```bash
scripts/test_update.sh apply --ref origin/main
```

一次性 `baseline-*` 不再用于同历史更新，也不作为 production 发布、正式 Key/号码维护或
管理员初始化入口。
