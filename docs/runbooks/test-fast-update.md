# 真实联调测试环境快速更新手册

## 目标和分类矩阵

快速更新只缩短测试环境发布路径，不改变真实厂商控制面、不接触凭据。它只
为**无迁移**更新提供上一版应用镜像的自动回退，不回退 schema，也不提供 Mock fallback。

| 分类 | 典型范围 | 处理 |
|---|---|---|
| `web-only` | `frontend/` 页面与静态资源 | 不等待托管 CI，构建 amd64 Web 镜像并替换；失败自动回退旧 Web 镜像 |
| `backend-safe` | 非高风险 `backend/` 应用代码 | 不等待托管 CI；无迁移不做数据库 checkpoint，失败自动回退旧应用镜像 |
| `high-risk` | 认证/授权/审计、加密/PII、发送/厂商链路、Compose、vendor-test/test-update 控制面 | 托管 CI/G2 异步运行；远端严格暂停并检查 submitting/retrying/uncertain |
| migration | Alembic/schema 变化 | 托管 CI/G2 异步运行；远端仅接受 expand-only，并只在此时创建密文 checkpoint |
| unknown/destructive | 未知路径、收缩/删除/重命名迁移 | fail closed，单独评审 |

`backend-safe` 与 `web-only` 同时出现时按组合更新处理；数据库仍只允许 expand-only。任何
high-risk 命中都会提升整次更新的门禁，不拆分绕过、不静默降级为普通更新。所有 high-risk
远端阶段都绑定同一 root-owned 不可变 host-control 快照；若该快照 commit 到目标 commit
之间的 host-control 资产字节未变，driver 可复用快照；若发生变化，必须先按
`deploy/README.md` 安装同目标 commit 的快照。apply 切换 checkout 后，verify/status 仍由
原快照裁决。不得用 raw Git、手工 Compose 或普通上传绕过。

固定 host-control 资产表同时属于路径分类合同：全部资产在普通更新中均为 high-risk，
public cutover 仅可剥离明确列出的发布元数据。CI 会逐项比较 shell driver 与 Python 合同
中的资产集合，新增或删除资产只改一侧都会失败。分类通过后仍必须执行 source commit
字节比较；“已归为 high-risk”不能替代同目标 commit 的 root-owned 快照安装。

## 本地入口

本地工作树必须干净，目标必须是已推送的 `origin/` ref。只配置非敏感连接信息：

```bash
scripts/docker_public.sh doctor
export SMS_TEST_UPDATE_TARGET='operator@test-host'
export SMS_TEST_UPDATE_PORT='22'
scripts/test_update.sh --dry-run --ref origin/main
scripts/test_update.sh --ref origin/main
```

部署根目录固定为 `/opt/sms-platform`，由脚本内置并在任何 Git 或 SSH 更新动作前校验。
日常操作不得设置其他根目录，也不得从历史会话、旧交接记录或个人 shell 配置复制
`SMS_TEST_UPDATE_ROOT`；即使旧路径仍存在且包含合法 Git 仓库，driver 也必须 fail closed。
通过连接校验后，driver 会在差异分类前输出非敏感的 `root/base/target/ref` 预检身份；
操作者应先确认 root 为规范目录、base 为当前测试基线，再依据分类结果继续。

手机远程 Mac 或其他无桌面会话无需解锁 macOS Keychain；driver 会自动进入一次性
public Docker 会话并匿名访问公开基础镜像。不得删除或改写全局 Docker 配置，也不得
把私有仓库凭据传给快速更新入口。`doctor` 未通过时先修复 Docker daemon、Compose、
Buildx 或公开网络，不得绕过自检继续构建。

脚本验证本地与远端属于同一 GitHub 仓库后，直接读取远端 status 与 migration head，
并由 CI 和测试发布共用的 Python 合同按实际差异分类；它不查询或等待托管 CI。driver 不重复
pytest、Ruff/Mypy、前端测试或完整 CI/G2，只构建受影响的 `linux/amd64` 镜像，校验不可变
image ID 和归档 SHA-256。归档与请求仅放入 root `0700` incoming 目录，文件为 `0600`。
大归档先写入按目标 commit 隔离的远端 `0700` staging，使用固定文件名的 `.part` 和
`rsync --partial --inplace` 最多重试三次；连接中断保留同一 `.part` 供本次或
同目标重跑续传。远端摘要一致后才原子发布，且 `request.json` 最后发布。
完整 G2 只由 CI 的共享风险分类触发；driver 最后严格解析 status，只有 update id、
`verified` 状态、目标 commit 和 migration head 四项同时匹配才以 0 退出。

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

从旧仓库一次性切换到新的规范仓库不属于日常快速更新：先在本地只读取得测试机精确
基线对象，确认测试机可读取新仓库及其目标分支，再按变更记录切换
`/opt/sms-platform` 的 `origin`。切换后立即从干净、已推送的规范分支重跑本入口并取得
verified 终态；不要通过旧部署根目录、手工 Compose 或直接 checkout 绕过首次差异分类。

若新规范仓库由 `PUBLIC-SNAPSHOT.json` 声明的无历史公开快照起根，旧基线与
`origin/main` 必然没有共同祖先。若目标快照同时把旧单 Redis/`sms_app` 拆为三域
Redis/七职责数据库角色，必须先取得操作者对“初始化 11 个新凭据”的明确确认，按目标
commit 重装下文 root-owned host-control 快照，再在服务器执行一次固定 bootstrap：

```bash
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  SMS_PUBLIC_CUTOVER_CONFIRMED=1 \
  /usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap \
  test-update bootstrap-public-cutover
```

bootstrap 只在服务器本机生成缺失的 11 个独立随机值，不输出值、长度、摘要或派生信息；
它把旧权威凭据和根 `.env` 复制到 root-only 备份目录，保留旧 runtime generation、
PostgreSQL 数据、全部 Docker volume 与运行态目录。它先保留旧 broker，新增并验证
auth/control 两域；任一步失败会停止新域、恢复旧权威凭据、旧 runtime target 和旧
Redis image 标签，同时保留失败现场。成功返回 `status=ready` 后，才使用同一入口的显式
一次性模式：

```bash
SOURCE_COMMIT="$(python3 -c 'import json; print(json.load(open("PUBLIC-SNAPSHOT.json"))["source_commit"])')"
git fetch <已授权的归档源仓库URL> "$SOURCE_COMMIT"
git cat-file -e "$SOURCE_COMMIT^{commit}"
scripts/test_update.sh --public-snapshot-cutover --ref origin/main
```

归档源 URL 只由已授权操作者在本地提供，不写入仓库、服务器 origin、日志或更新请求。
该模式固定只接受 `origin/main`，要求测试基线与 manifest 源提交存在唯一共同私有祖先，
并用 manifest 锁定的公开策略从源提交重新导出无历史快照；只有重新导出的完整文件树与
公开历史中唯一 publication commit 逐文件、执行位和符号链接完全一致，才会把私有证据
删除及公开发布元数据从逻辑运行差异中剥离。测试基线与源提交即使在共同祖先后分叉，
两者之间的全部公开运行文件差异也仍会进入分类。其余应用差异继续走原分类器；运行时
secrets/release/reset 控制文件一律提升为 high-risk，并要求目标 commit 的 CI 已执行完整
G2。普通同历史更新传入
该参数会失败，日常流程不得保留或复用这个参数。

公有仓库不会携带 manifest 指向的私有源 Git 对象。driver 在本地验真后只生成包含该
source commit 与其**当前文件树对象**的最小 Git pack，不携带私有提交历史；pack 由
source commit、private merge-base 和 SHA-256 同时绑定，并先于 request 原子发布。远端
root-owned 控制器只在私有临时 bare repository 中导入，要求 pack 的对象集合精确等于
source commit 加完整当前树，再借助现有基线/公有对象重建快照和计算逻辑差异。无论验真
成功或失败，pack 都在 prepare 内删除，不导入 `/opt/sms-platform/.git`，也不把归档源
URL、私有 ref 或私有历史写入更新请求。证据缺失、摘要不符、包含额外对象或清理失败均
fail closed。

high-risk 首次运行依赖 host-control 安装器准备固定 `/etc/sms-platform/test-update-backup.json`、本机随机生成的 root `0600` 备份密钥和 `/var/lib/sms-platform/test-backups`。安装器在修改任何 checkpoint 权威状态前取得固定 lifecycle lock，冲突时不修改 config、key 或 checkpoint 输出目录；只在 config/key 均不存在且输出目录不存在或为空时生成一次，重装必须保留原密钥。唯一允许恢复的半状态是 key 已安全落盘、config 未提交且输出目录仍为空，此时只补 config。config 原子出现后即已提交；若后续目录落盘或身份复核失败，当次仍 fail closed，下一次必须完整复验并重新 `fsync` 权威目录才可接受。config-only、权限/硬链接漂移、中断或非法 checkpoint、其他半安装一律 fail closed。不得手工删除密钥后重跑、不得把密钥复制进仓库、命令参数、环境变量、日志或聊天。

pre-live 的 `prepare` 还会协调旧测试环境的根 `.env`：仅允许整行移除账号 Provider 已废弃的 LDAP/隐藏管理员键（不再解释其旧值），以及去掉语义不变的行尾说明；协调后的固定五项必须仍精确等于纯 Mock 合同才原子替换。它不会把非 Mock 配置改回 Mock，也不会读取、生成或迁移任何正式 Key。未知键的非严格语法、任何重复键或凭据键一律 fail closed。

## 日常标准流程

开发测试阶段只使用已经完成五次连续真实验证的入口，不增加二次包装命令。标准顺序如下：

1. 完成开发和必要测试。
2. 提交修改，确认本地工作树干净。
3. 推送目标分支到 `origin`。
4. 执行 `scripts/test_update.sh --ref origin/<branch>`。
5. 确认最终 `test-update status` 返回 `state=verified`。
6. 完成针对性验收：前端功能使用浏览器检查，API 功能使用对应接口检查。

普通 `web-only` 更新从命令启动到 `verified` 的五连发实测平均约 1 分 20 秒。这个时间只用于操作预期，不是超时或成功判据。脚本会自动进入一次性 public Docker 会话；`scripts/docker_public.sh doctor` 用于首次使用或故障诊断，不要求每次更新前重复运行。`--dry-run` 可用于评审分类，但不是日常更新的强制前置步骤。

无迁移更新在镜像替换或 verify 失败时自动恢复上一版 commit 与镜像，恢复健康后释放本次
暂停并记录 `state=rolled_back`；脚本仍以失败退出，修复代码后重新提交、推送并重跑同一
入口。迁移更新、受保护状态异常或自动回退失败时保持 fail closed。若旧失败更新持有
`test-update:<old-id>` 暂停，新 prepare 只在同一 lifecycle lock 内确认旧 state 为
`blocked` 且安全边界通过后，才原子接管两条 lane；manual/daily/其他 critical 暂停不可
接管。

快速更新始终保留 PostgreSQL 数据库、Docker volume、运行态目录和真实联调控制状态。任何初始化必须事先取得操作者明确确认；管理员初始化是独立管理流程，不得作为更新步骤自动执行。正式厂商 Key 的安装或轮换、真实测试号码管理及真实联调激活也不属于快速更新输入，只能走各自受控入口。

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
  校验 expand-only 并创建仅密文 checkpoint。
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
