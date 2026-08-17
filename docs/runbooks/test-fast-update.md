# 真实联调测试环境快速更新手册

## 目标和分类矩阵

快速更新只缩短测试环境发布路径，不改变真实厂商控制面、不接触凭据。它只
为**无迁移**更新提供上一版应用镜像的自动回退，不回退 schema，也不提供 Mock fallback。

| 分类 | 典型范围 | 处理 |
|---|---|---|
| `web-only` | `frontend/` 页面与静态资源 | CI 并行运行；构建 amd64 Web 镜像并替换，失败自动回退旧 Web 镜像 |
| `backend-safe` | 非高风险 `backend/` 应用代码 | CI 并行运行；无迁移不做数据库 checkpoint，失败自动回退旧应用镜像 |
| `high-risk` | 认证/授权/审计、加密/PII、发送/厂商链路、Compose、vendor-test/test-update 控制面 | `apply` 前要求目标 commit 精确 `ci-gate=success`；远端严格暂停并检查 submitting/retrying/uncertain |
| migration | Alembic/schema 变化 | `apply` 前要求目标 commit 精确 `ci-gate=success`；远端仅接受 expand-only，并只在此时创建密文 checkpoint |
| unknown/destructive | 未知路径、收缩/删除/重命名迁移 | fail closed，单独评审 |

`backend-safe` 与 `web-only` 同时出现时按组合更新处理；数据库仍只允许 expand-only。任何
high-risk 命中都会提升整次更新的门禁，不拆分绕过、不静默降级为普通更新。所有 high-risk
远端阶段都绑定同一 root-owned 不可变 host-control 快照；若该快照 commit 到目标 commit
之间的 host-control 资产字节未变，driver 可复用快照；若发生变化，必须先按
`deploy/README.md` 安装同目标 commit 的快照。apply 切换 checkout 后，verify/status 仍由
原快照裁决。不得用 raw Git、手工 Compose 或普通上传绕过。

固定 host-control 资产表同时属于路径分类合同：全部资产在普通更新中均为 high-risk。
CI 会逐项比较 shell driver 与 Python 合同中的资产集合，新增或删除资产只改一侧都会失败。
分类通过后仍必须执行 source commit 字节比较；“已归为 high-risk”不能替代同目标 commit
的 root-owned 快照安装。

## SSH 更新用户与 sudo 面

日常 SSH 更新用户对 `sms-compose` / host-control 入口的 `sudo` 面等价于 root：能切换代码、重建容器、操作暂停键与密文 checkpoint。必须按完全可信管理员管理该账号（密钥、登录来源、审计），**不能靠受限 sudoers 规则把它降为“半特权运维”**；任何能调用上述固定入口的 sudo 授权都视为完整管理权。

## 本地入口

本地工作树必须干净，默认目标是已合并的精确 `origin/main`。首次使用把样例复制为 Git 忽略的
本地文件；该文件只允许两个连接键和一个非敏感厂商 origin，必须为当前用户拥有的 0400/0600
普通文件。键名可以公开，真实 target/port 值属于受限运维信息，不得进入 Git、文档、聊天或
命令历史；`SMS_VENDOR_LIVE_TEST_ORIGIN` 不包含凭据，但必须填写与测试环境联调标记一致的
HTTPS origin：

```bash
scripts/docker_public.sh doctor
gh auth status --hostname github.com
cp test-update.env.example .env.test-update
chmod 600 .env.test-update
# 编辑 SMS_TEST_UPDATE_TARGET、SMS_TEST_UPDATE_PORT 与 SMS_VENDOR_LIVE_TEST_ORIGIN
scripts/test_update.sh apply --ref origin/main
```

driver 会把该 origin 作为固定环境参数传给远端 `sudo` 控制入口，避免管理器用占位地址校验
已激活的测试环境标记；不需要手工 SSH 或修改服务器文件。

`apply` 已严格解析最终 `state=verified`；`plan --ref origin/main` 仅用于变更前预览，
`status` 仅用于后续只读诊断。分支 ref 只用于明确的合并前验收例外，不是默认入口。

部署根目录固定为 `/opt/sms-platform`，由脚本内置并在任何 Git 或 SSH 更新动作前校验。
日常操作不得设置其他根目录，也不得从历史会话、旧交接记录或个人 shell 配置复制
`SMS_TEST_UPDATE_ROOT`；即使旧路径仍存在且包含合法 Git 仓库，driver 也必须 fail closed。
通过连接校验后，driver 会在差异分类前输出非敏感的 `root/base/target/ref` 预检身份；
操作者应先确认 root 为规范目录、base 为当前测试基线，再依据分类结果继续。

手机远程 Mac 或其他无桌面会话无需解锁 macOS Keychain；driver 会自动进入一次性
public Docker 会话并匿名访问公开基础镜像。不得删除或改写全局 Docker 配置，也不得
把私有仓库凭据传给快速更新入口。`doctor` 未通过时先修复 Docker daemon、Compose、
Buildx 或公开网络，不得绕过自检继续构建。

GitHub 鉴权与 Docker 鉴权是两个独立边界。`apply` / `promote` 在任何构建或服务器变更
前执行 `gh auth status --hostname github.com` 与目标仓库只读 API 预检；精确 CI 查询会
优先使用已有环境 token，否则在内存中读取 GitHub CLI 系统钥匙串 token，绝不回显。
鉴权失效时运行 `gh auth login --hostname github.com --web` 走官方设备/浏览器登录；
不得复制 token 到聊天或配置文件，也不得长期 `export GITHUB_TOKEN`/`GH_TOKEN`。

脚本验证本地与远端属于同一 GitHub 仓库后，先以 SSH 更新用户执行远端 Git 读路径预检
（`remote get-url origin`、`rev-parse HEAD`、`status --porcelain`），再读取 migration head；
远端 `.git` 不可由该用户读/遍历时立即 fail closed，并提示修复 operator Git 权限，
不得改用 root Git 结果冒充日常更新用户可用，
并由 CI 和测试发布共用的 Python 合同按实际差异分类。普通 `web-only`/`backend-safe`
无迁移更新允许 CI 并行运行；high-risk、迁移或 host-control 更新必须通过 GitHub 公共 API
验证目标 commit 上由 GitHub Actions 应用产生的精确 `ci-gate=success`，同名第三方状态
不能通过。driver 不重复 pytest、Ruff/Mypy、前端测试或完整 CI/G2，只构建受影响的
`linux/amd64` 镜像，校验不可变 image ID 和归档 SHA-256。归档与请求仅放入 root `0700`
incoming 目录，文件为 `0600`。
大归档先写入按目标 commit 隔离的远端 `0700` staging，使用固定文件名的 `.part` 和
`rsync --partial --inplace` 最多重试三次；连接中断保留同一 `.part` 供本次或
同目标重跑续传。远端摘要一致后才原子发布，且 `request.json` 最后发布。
完整 G2 只由 CI 的共享风险分类触发；verify 后 driver 还必须以日常更新用户重新检查
远端 operator 的 origin、HEAD、tracked 工作树与暂存区读路径，任一检查失败都拒绝记录成功；
控制面切换后只修复 tracked 文件的 group 读权限，不触碰 ignored secrets/data。driver 最后
严格解析 status，只有 update id、`verified` 状态、目标 commit 和 migration head 四项同时
匹配才以 0 退出。

迁移 PR 不只验证“本次新增 revision”。CI 从受控测试环境兼容基线
`0021_approval_legacy_default` 一直检查到仓库唯一 head；因此新增 head 会自动进入完整
迁移列车。静态检查无法证明的动态 SQL 必须改写成逐条字面量 SQL。替换触发器或约束时，
删除和重建必须在同一迁移内成对出现；删表、删列、删数据、类型收缩和重命名仍属于
destructive。滚动窗口需要改名时先采用双列/双写/read-new，旧 writer 全部退场并取得
受控验证证据后，再以独立 contract 变更处理旧列，不得在 expand 阶段直接 rename。

差异分类前，driver 会把本地与 `/opt/sms-platform` 的 `origin` 规范化为 GitHub
`owner/repository` 身份并要求完全一致；任何跨仓库、带密码 URL 或非 GitHub 来源均
fail closed。远端当前 HEAD 也必须已经存在于本地对象库；缺失基线、`git diff` 出错或
差异存在但路径枚举为空都属于错误，绝不能解释成“无运行时代码差异”。

从旧仓库一次性切换到新的规范仓库不属于日常快速更新。如果服务器 HEAD 不在当前公开
仓库对象库，driver 会在差异分类前失败关闭；这是预期安全状态。禁止在本公开工作区添加
私有归档 remote、fetch 私有 commit、创建临时 ref、生成 Git pack，或调用已禁用的
`--public-snapshot-cutover` 参数。

跨历史基线迁移必须另立变更单，在**不属于本公开工作区**的隔离临时证据仓库和受控服务端
维护窗口中完成。方案至少要经过：私有证据访问授权、公开快照逐文件复验、数据/volume
保留与回退设计、24 件 secrets/三域 Redis/七职责数据库角色迁移评审、清理证明，以及
服务器最终 HEAD/origin 都绑定公开仓库 commit 的复核。任何私有 URL、ref、commit 或
Git 对象不得回流到公开工作区。完成新公开基线后，先运行只读 `status`，再从干净且已合并
的 `origin/main` 恢复本手册的按需 `apply` 流程；不得用 raw Git/Compose 绕过。

high-risk 首次运行依赖 host-control 安装器准备固定 `/etc/sms-platform/test-update-backup.json`、本机随机生成的 root `0600` 备份密钥和 `/var/lib/sms-platform/test-backups`。安装器在修改任何 checkpoint 权威状态前取得固定 lifecycle lock，冲突时不修改 config、key 或 checkpoint 输出目录；只在 config/key 均不存在且输出目录不存在或为空时生成一次，重装必须保留原密钥。唯一允许恢复的半状态是 key 已安全落盘、config 未提交且输出目录仍为空，此时只补 config。config 原子出现后即已提交；若后续目录落盘或身份复核失败，当次仍 fail closed，下一次必须完整复验并重新 `fsync` 权威目录才可接受。config-only、权限/硬链接漂移、中断或非法 checkpoint、其他半安装一律 fail closed。不得手工删除密钥后重跑、不得把密钥复制进仓库、命令参数、环境变量、日志或聊天。

pre-live 的 `prepare` 还会协调旧测试环境的根 `.env`：仅允许整行移除账号 Provider 已废弃的 LDAP/隐藏管理员键（不再解释其旧值），以及去掉语义不变的行尾说明；协调后的固定五项必须仍精确等于纯 Mock 合同才原子替换。它不会把非 Mock 配置改回 Mock，也不会读取、生成或迁移任何正式 Key。未知键的非严格语法、任何重复键或凭据键一律 fail closed。

## 按需标准流程

只有需要共享测试环境验收时才执行本节；纯文档、纯测试和不改变行为的重构无需部署。
标准顺序如下：

1. 完成开发并运行 `scripts/dev_check.sh --changed`。
2. 提交修改，确认本地工作树干净。
3. 推送目标分支到 `origin`，确认自动 Draft PR 已创建；若 PR 落后于 main，自动化会明确
   失败并提示合并 main 后重新推送（`GITHUB_TOKEN` 更新分支不会触发 push CI），新 push CI
   成功后自动 Ready 并请求 squash merge。
4. 确认 `gh auth status --hostname github.com` 有效；失效时只走官方设备/浏览器登录。
5. 获取自动合并后的最新 `origin/main`，确认其 SHA 正是本次 PR 的 squash 结果。
6. 如需预览，执行 `scripts/test_update.sh plan --ref origin/main` 查看分类和门禁。
7. 执行 `scripts/test_update.sh apply --ref origin/main`。
8. 命令成功退出已经证明最终 `test-update status` 返回 `state=verified`；随后完成针对性验收：
   前端功能使用浏览器检查，API 功能使用对应接口检查。自动合并仅完成
   仓库集成，不替代测试环境的 `verified` 判据。

普通 `web-only` 更新从命令启动到 `verified` 的五连发实测平均约 1 分 20 秒。这个时间只用于操作预期，不是超时或成功判据。脚本会自动进入一次性 public Docker 会话；`scripts/docker_public.sh doctor` 用于首次使用或故障诊断，不要求每次更新前重复运行。`--dry-run` 可用于评审分类，但不是日常更新的强制前置步骤。

`plan` 不构建、不上传、不修改服务器；`build` 只完成本地缓存/镜像准备；`status` 只读且
仅在需要稍后诊断时运行；
`apply` 成功后写入 GitHub `test` Environment Deployment。`promote` 只接受 `origin/main`，
要求 main 的精确 CI 证据与相同 tree；任一不满足都失败关闭并要求重新执行 `apply`，
需要预览时可先运行 `plan`。

无迁移更新在镜像替换或 verify 失败时自动恢复上一版 commit 与镜像，恢复健康后释放本次
暂停并记录 `state=rolled_back`；脚本仍以失败退出，修复代码后重新提交、推送并重跑同一
入口。迁移更新、受保护状态异常或自动回退失败时保持 fail closed。若旧失败更新持有
`test-update:<old-id>` 暂停，新 prepare 只在同一 lifecycle lock 内确认旧 state 为
`blocked` 且安全边界通过后，才原子接管两条 lane；manual/daily/其他 critical 暂停不可
接管。

快速更新始终保留 PostgreSQL 数据库、Docker volume、运行态目录和真实联调控制状态。任何初始化必须事先取得操作者明确确认；管理员初始化是独立管理流程，不得作为更新步骤自动执行。正式厂商 Key 的安装或轮换、真实测试号码管理及真实联调激活也不属于快速更新输入，只能走各自受控入口。

### 同历史基线重对齐例外

服务器 commit 是目标 `origin/main` 的祖先、服务器迁移头落后，且普通 `apply` 只被合同中
固定的旧运行凭据准备/撤销脚本与非运行态文件阻断时，可另行评审后执行一次：

```bash
scripts/test_update.sh rebaseline --ref origin/main
```

执行前必须按 `deploy/README.md` 的“一次性主机安装”重装目标 commit 的 root-owned
host-control 快照，并确认该 commit 的 `backend`、`frontend`、`security`、`g2`、
`ci-gate` 五个 check 全部由 GitHub Actions 成功完成。入口使用独立严格分类器，要求两个
固定运行控制路径和至少一个迁移路径同时存在、迁移头真实前移，并强制 API/Web 双镜像；
随后完整复用 high-risk 的暂停、`uncertain` 拦截、密文 checkpoint、expand-only 检查、
prepare/apply/verify/status 和 operator Git 复核。当前唯一批准的
`0053_idempotency_scope → 0061_vendor_binding_outbox` 重对齐还会在 lifecycle lock 内把
权威运行密钥从精确旧 18 件合同扩展到 24 件：仅追加四个两两独立的审计 context key 和
一对匹配的 X25519 告警凭据 key，由服务器本机生成并逐文件 `O_EXCL`/`fsync` 提交；不会
输出值、长度、摘要或派生信息，不会替换既有 18 件密钥或厂商凭据。完整 24 件清单时幂等
复核，合法的 private-first 中断可续写公钥；缺件、额外文件、错误权限、重复审计 key、
public-only 或不匹配 keypair 一律失败关闭。密文 checkpoint 完成后才扩展权威清单，切换
目标源码后再由目标提交的固定预处理器原子生成新 runtime generation，然后才允许迁移。

无共同历史继续使用独立 public baseline 流程；无迁移、非 `origin/main`、缺少完整 CI、
host-control commit 不一致或出现任意额外禁止路径时，`rebaseline` 必须失败关闭。完成本次
重对齐后恢复日常 `apply`，不得把该入口用于普通分类器拒绝的未来变更。

如果这一唯一重对齐在目标 checkout 已激活、但数据库迁移尚未开始时阻断，状态必须是
`blocked/migrate`，实际 migration head 仍为 `0053_idempotency_scope`，两条 update pause
继续由该 update id 持有。只有同时验证这组状态、目标 HEAD、旧 API/Web 运行镜像标签和旧
migration head 后，才可用同一目标 commit 的 host bootstrap 执行一次：

```bash
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap \
  test-update recover-rebaseline
```

该恢复只把 `.env` 的 API/Web 镜像指针重新绑定到仍在运行且标签匹配的旧镜像，并把 tracked
checkout 恢复到请求 base；不启动容器、不迁移/回退 schema、不释放 pause、不删除旧状态
日志。成功后必须重新执行完整 `scripts/test_update.sh rebaseline --ref origin/main`，由新
update id 原子接管旧 blocked pause；任何身份不一致都停止并保留 fail-closed 状态。
若首次恢复已返回 `recovered`，但随后的 operator Git 读路径复核被中断，可原样重放同一
`recover-rebaseline` 命令。入口仅接受 checkout 仍在请求 target 或已经回到请求 base 的两种
精确身份，并在最后一次 root Git 校验之后重新恢复 `.git` 与 tracked 工作树的 group 读权限。

若 rebaseline 已完成目标 checkout、`0061_vendor_binding_outbox` 迁移与 API/Web 切换，但唯一
阻断是 `blocked/get_balance`，不得回退 schema、手工改状态或清除 pause。先重装准备恢复的
新目标 commit host-control 快照；只有快照 commit、请求 target、实际 HEAD 和实际 migration
head 全部精确一致，原请求确为上述 rebaseline，状态错误类型为 `invariant_failed`，且两条
update pause 仍由原 update id 持有时，才可执行：

```bash
scripts/test_update.sh recover-rebaseline-verify
```

本地入口会从权限收紧的 `.env.test-update` 读取并校验测试服务器、端口和
`SMS_VENDOR_LIVE_TEST_ORIGIN`，再把固定运行目录与厂商 origin 一并传给不可变 host
bootstrap；不得手工拼接缺少 origin 的远端命令。

入口先把原状态原子推进到 `verifying/recover_verify`，再从环境模式、预算守恒、pause 所有权、
真实 GetBalance、服务健康与 migration head 开始完整重跑 verify；GetBalance 成功后、服务健康
检查前，在两条原 update pause 仍被持有时仅以 `up -d --no-deps` 启动固定 backend 服务集合，
并在活动根、baseline manifest、镜像和 operator Git 身份精确匹配后，由 baseline manager
仅从已知旧/目标 unit 字节恢复目标 `vendor-control-agent.service`，重启并验证安全投影；不得手工
`chown` 或直接启动 systemd unit。不强制重建、不重跑迁移、不切换镜像、不更换凭据。unit 恢复、
启动或任一后续不变量再次失败会停止发送
进程、重新进入 `blocked` 并保持 fail closed。
若命令在恢复中断，可原样重放；`verifying/recover_verify` 会继续完整 verify。若完整 verify
再次阻断，只有不可变事件链能证明紧邻的 `blocked → verifying/recover_verify → blocked` 且
失败 step 属于固定 verify 步骤时才可重放；已 `verified` 则只复核精确身份后幂等返回。缺少
上述来源证明的其他 blocked step、其他迁移、普通 apply 或非 host bootstrap 调用一律拒绝。

## 服务端固定阶段

`sudo` 不继承 systemd 的 `/etc/sms-platform/compose.env`。本地 driver 因此会以固定
argv 显式传递以下四个非敏感 host-control 变量；不得依赖交互 shell 的环境，也不得在
这里加入凭据值或测试号码。以下数组仅用于普通 web/backend-safe 人工诊断；high-risk
由 driver 把最后一个程序固定替换为同一份
`/usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap`，四阶段不能
中途切换：

```bash
SMS_COMPOSE=(
  sudo /usr/bin/env
  SMS_PLATFORM_ROOT=/opt/sms-platform
  SMS_SECRETS_MODE=development
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials
  /usr/local/sbin/sms-compose
)
"${SMS_COMPOSE[@]}" test-update prepare
"${SMS_COMPOSE[@]}" test-update apply
"${SMS_COMPOSE[@]}" test-update verify
"${SMS_COMPOSE[@]}" test-update status
```

- `prepare`：取得 lifecycle lock，验证 marker、请求、Git base/target、归档和 amd64
  image ID；原子暂停两条 lane。普通无迁移更新拒绝 submitting/retrying，但允许已有
  uncertain 由 reconcile 继续收敛；high-risk 或迁移更新额外拒绝 uncertain。有迁移时才
  校验 expand-only 并创建仅密文 checkpoint。prepare 无论在 root Git
  fetch/checkout 前置阶段还是后续门禁阶段失败，都会先保持 fail closed，再
  恢复 `.git` 与 tracked worktree 的 operator 读路径；恢复失败仍以 blocked 退出，不能记录
  成功或要求人工 chmod/chown 作为日常流程。
- `apply`：有迁移时先执行迁移；随后保存旧运行镜像身份、切换目标 commit 与不可变镜像，
  仅重建受影响服务；全过程不启动 mock-vendor、不触碰数据 volume。
- `verify`：核对环境 mode、PostgreSQL recipient/迁移 head、服务集合、账本守恒和 pause
  所有权；只有 live 模式调用 GetBalance，pre-live 明确跳过。全部通过后只清除本次 update
  自己创建或继承的 update pause。
- `status`：只读取固定状态，不输出 secret、号码/HMAC 或正文。

无迁移失败只自动回退应用 commit/镜像，不切回 Mock、不清库、不删除 volume；迁移后失败
不自动回退 schema，继续 fail closed。包装器仍没有任意 restore、数据恢复或初始化命令。

## 数据与验收

更新始终保留数据库和 volume；不得用 `down -v`、重建空库或删除 runtime 目录伪造通过。
验证失败时记录 update id、commit、阶段、迁移 head、容器健康、计费条计数和无 PII 错误码，
然后修复代码重新走 prepare → apply → verify。不要把密钥值或元数据、手机号/HMAC、正文或
recipient 加密字段写入诊断输出。
