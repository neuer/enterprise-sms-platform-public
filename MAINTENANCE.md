# MAINTENANCE.md — 安全且高效的日常交付

平台已进入维护期，本文件是唯一日常流程入口。完整 G2 仅用于受保护变更、专项复验和
生产候选，不进入普通编码循环。

`PROGRESS.md` 只登记需要仓库外状态变化或操作者协调才能解除的活跃阻塞；普通提交、
PR、CI 和已完成状态不回填。PR 与 CI 事实以 GitHub 为准，发布不可变证据以生产变更单
和 release manifest 为准；无活跃阻塞时只保留明确空态。

## 日常开发

首次克隆或新 worktree 启用 git pre-commit / pre-push。这是本机门禁的启用步骤，
不是 Cursor Settings → Hooks：

```bash
scripts/install_git_hooks.sh
```

`git commit` 走 `.githooks/pre-commit`，`git push` 走 `.githooks/pre-push`，按变更路径
选检查，不是一刀切。`git commit --no-verify` 会绕过 git hook，不要用。
编码过程中直接运行与改动相关的 pytest/Vitest 或局部静态检查。准备提交时再执行一次：

```bash
scripts/dev_check.sh --changed
git add <本次文件>
git commit
git push -u origin <branch>
```

`dev_check --changed` 是提交前组件检查，不要求每次保存后运行，也不触发测试服务器部署。
检查在临时 Git 候选工作树中执行：dev 捕获工作内容，commit 捕获实际 index，push
逐个检查 Hook stdin 中的提交 SHA。不会把未暂存的修正算作已暂存代码通过。
同一候选 tree、变更基线、检查选择和工具版本的成功结果只在本机缓存，后续 commit/push
可复用；内容变化或检查失败不会写成功缓存。临时 index 不修改操作者的暂存区。
后端依赖使用 `uv --locked`，不允许检查过程改写锁文件。

推送时，`pre-push` Hook 会扫描工作区和即将公开的提交，只报告文件/规则而不回显命中内容。
owner 分支会自动创建 Draft PR；当前公开仓库没有自动 Ready/合并工作流。
精确 push CI 成功并完成独立 Code Review 后，由操作者将 PR 改为 Ready 并请求
squash merge。required `ci-gate`、required reviews、会话解决和冲突保护全部满足后
才能合并，禁止管理员绕过。若 PR 落后于 main 或存在冲突，先在任务分支处理并重新
推送，再核验新 head SHA 的 CI 与评审状态。
合并后核对 PR 的实际 merge SHA 与 `origin/main`，并验证该 SHA 的 GitHub Actions
`ci-gate=success`；PR head 的绿色结果不能直接当作合并提交通过。当前 CI 保留受信任
PR 证据复用和内部 dispatch 校验能力，但没有自动创建一次性 tag 或派发合并验真的
工作流，不能假定这些动作已经发生。若合并提交没有 CI，使用现有 `workflow_dispatch`
执行完整 CI，并确认运行绑定目标 SHA；不要手工填写内部 `post_merge_*` 参数。
清理已合并分支前检查远端 ref，存在后续提交时保留；合并本身不代表测试部署完成。

CI 每次独立运行检查，不读取本地免检回执。前端与门禁契约在 changes 中执行，
前端专用变更也能触发 Python 契约。CI 控制入口变更覆盖全部相关 job；README 和
CONTRIBUTING 等普通根文档走廉价合同检查，未知源代码路径仍保守全检。
后端单元测试与隔离 PostgreSQL/Redis 测试并行，各自上传同一 SHA 的实际执行清单、
JUnit 和覆盖数据。隔离分区自动收集 `tests/integration/` 以及显式真实 Redis 用例，
任何 skip 都失败；各集成模块从同一迁移基线克隆独立临时库，避免状态污染。
汇总重新独立收集完整清单，验证分区互斥且穷尽、关键 marker 实际通过，
并核验 app 文件全集及 application/auth/pipeline/export/tasks/api 六项覆盖率，
同时保留原 full G2 的 services ≥80% 基线。
full G2 复用 `scripts/run_backend_tests.sh full <临时证据目录>` 和相同静态入口，
不再重复执行关键 marker 子集。普通分区仅保留公开快照缺失文档与本机无 Lua 的明确例外。

CI 按风险运行组件门禁；认证/授权/审计、加密与 PII、发送/厂商、迁移、部署和控制面等
受保护变更进入 G2 integration，发布控制烟测仍按路径选择。性能压测不进入 PR、push、
每日定时或人工 CI/G2 门禁，只在隔离测试环境按 `docs/PERFORMANCE.md` 专项执行并单独归档证据。

## 按需测试部署

测试部署与日常开发解耦。只有需要共享环境、浏览器或真实接口验收的功能才部署；纯文档、
纯测试和不改变行为的重构无需部署。默认等待 PR 合并后，更新精确的 `origin/main`：

```bash
git fetch origin
scripts/test_update.sh apply --ref origin/main
```

`apply` 已完成差异分类、必要 CI 验证、构建、远端切换、verify、operator Git 读路径复核和终态解析；
只有远端返回 `state=verified` 且切换后 operator 再次通过 origin、HEAD、工作树检查才算成功。
任一读路径检查失败都会拒绝记录部署成功。需要在变更前预览分类时可运行
`scripts/test_update.sh plan --ref origin/main`，需要稍后查看非敏感状态时再运行
`scripts/test_update.sh status`，二者都不是每次部署的强制重复步骤。

首次使用测试部署时才准备本地连接文件和 GitHub CLI：

```bash
cp test-update.env.example .env.test-update
chmod 600 .env.test-update
gh auth status --hostname github.com
```

`.env.test-update` 只允许 `SMS_TEST_UPDATE_TARGET` 与 `SMS_TEST_UPDATE_PORT`，被 Git
忽略且必须为当前用户拥有的 0400/0600 普通文件。不得在仓库、命令历史或文档中记录真实
主机地址、账号、密钥或测试数据。鉴权失效时只用
`gh auth login --hostname github.com --web`；不得粘贴或长期导出 GitHub token。

### 测试服务器门禁

| 风险 | 测试部署 | 合并 |
|---|---|---|
| `web-only` / `backend-safe`、无迁移 | 合并后的 `origin/main` 按需 `apply` | 精确 CI 与独立评审通过后人工 Ready + squash |
| `high-risk` 或迁移/控制面 | `apply` 前必须有目标 commit 的精确 `ci-gate=success` | 精确 CI 与独立评审通过后人工 Ready + squash |
| 未知/破坏性迁移 | fail closed，拆分并单独评审 | 禁止 |

测试更新只构建受影响镜像，不重复组件测试或 G2；有迁移才创建密文 checkpoint。无迁移
切换/验收失败自动恢复旧 commit 与应用镜像并记录 `rolled_back`，绝不自动回退 schema。
`status` 是统一的只读诊断入口，只输出仓库、commit、test-update 与 vendor-test 的非敏感状态。
如果测试服务器基线不在当前公开仓库对象库，日常入口会失败关闭；不得向公开工作区添加
私有归档 remote、fetch 私有提交或生成私有对象包。此类跨历史迁移必须在隔离临时证据
仓库中另行设计、评审和执行，先建立新的公开基线，再恢复本文件的日常流程。
获批的一次性步骤只见
[`docs/runbooks/public-baseline-activation.md`](docs/runbooks/public-baseline-activation.md)；
它不是日常 `plan/apply/status` 的替代入口。本地只允许
`prepare → build → finalize`，服务器只允许
`baseline-prepare/apply/verify/status/finalize/cleanup`；`request.json` 必须最后上传。
服务器 operator UID/GID 固定为 `1000:1000`。finalize 后仍保留旧 root，只有
`state=verified` 且表面验收完成才运行 cleanup；cleanup 删除旧 root 与
bundle/API/Web 三个大产物，但保留 manifest/request、test-update store 和 core journal。

### 同历史测试基线重对齐

当服务器 commit 仍是目标 `origin/main` 的祖先，但跨越多个已审核迁移，且日常 `apply`
只因固定的旧运行凭据准备/撤销脚本与操作文档差异失败关闭时，允许一次性执行：

```bash
scripts/test_update.sh rebaseline --ref origin/main
```

该入口不放宽日常分类器，只接受合同中枚举的固定历史非运行态文件和普通快速更新路径；
两个旧运行控制脚本如出现在差异中必须成对出现，未发生差异时不要求制造伪变更。入口必须
同时存在真实迁移前移，并强制构建 API/Web。执行前必须按
[`deploy/README.md`](deploy/README.md) 的“一次性主机安装”把 root-owned host-control
快照绑定到同一目标 commit，且目标 commit 的 `backend`、`frontend`、`security`、`g2`
PR 证据必须已由合并提交的精确 `ci-gate` 绑定并验证成功。后续仍走 high-risk 的暂停、
`uncertain` 拦截、密文数据库 checkpoint、expand-only 迁移、应用镜像回退、verify 和
operator Git 复核；无共同历史、无迁移、任意新增禁止路径或非 `origin/main` 一律拒绝。

PR 按上述 CI 与独立评审流程合并后，只表示仓库集成完成，不代表测试服务器已验证；按需测试更新只针对合并后的精确 `origin/main`。
服务器基线或保护状态异常时仍失败关闭。分支部署只作为明确的合并前验收例外；若分支已经
通过人工验证，且 squash 生成的新 `main` commit 与已验证分支 commit 的 tree 完全一致，
仍可运行：

```bash
scripts/test_update.sh promote --ref origin/main
```

该命令要求 `main` 的精确 `ci-gate=success`，只切换服务器 Git 身份，不重建镜像、不迁移
数据库；若 tree 不一致则失败关闭，必须重新执行 `apply`，需要预览时可先运行 `plan`。
每次成功的 `apply` 或 `promote` 都写入 GitHub `test` Environment 的 Deployment 记录。

## 生产发布

2026-09-10 一次性跨版本例外：操作者明确批准以当天 09:45 的整机虚拟机快照承担人工恢复，
仅放行离线四镜像 `0084_auth_security_and_ad_freshness` → `0116_usage_release_generation`，
清单必须声明 `cold_cutover`，不得冒充 `expand`。生成包时必须显式接受
`--allow-offline-no-conditional-evidence` 才能省略本候选的数据库恢复演练证据；数据镜像验证、
精确 main、CI、四镜像扫描、来源证明与包签名仍必须通过。此例外不扩展到其它版本或日常发布。
激活失败或激活中断后再请求 resume，固定停止 web、API、worker、outbox 与 beat，进入
`recovery_required`，不自动恢复旧应用或降级数据库。操作者负责整机恢复；快照不能撤销已发送
短信、已消费报告或已投递回调，开放业务后的故障必须先核对外部副作用，不能直接恢复旧事实后重发。
迁移到目标后，受控入口记录并设置旧 API Key 的有限候选为
`legacy_data_hmac_pepper_v1,legacy_sha256`，仅允许原值为空或同值；其它值冲突即停机。
执行前必须准备独立 pepper，并保留旧 data_hmac_key 文件原文作为 legacy secret；不批量改写摘要。
宿主包更新仍独立执行。本例外优先于下述旧离线迁移闭集及部署索引中的“不扩展”限制。

1. 最终候选必须是受保护 `main` 的精确 SHA。质量、安全与 G2 在日常合并流程完成；临时
   Release Gate 只负责制品生成，不再重复这些门禁。
2. 内部 Registry 建成前，临时 Release Gate 只构建四镜像一次，对每个镜像执行独立 Trivy
   HIGH/CRITICAL 扫描并生成候选 SBOM、archive 和单构建检查点；不执行第二次可重复构建，索引
   明确记录 `reproducibility_proven=false`。关闭阶段失败只重跑失败 job，并复用同一检查点。
3. 临时使用**生产离线 Docker image archive 发布包（镜像 OCI-compatible，不是 OCI Image Layout）**：GitHub 生成候选、核验 attestation 后，由受控
   签名环境生成并签署封闭包；通常同一包先通过预生产，再由远端发布 driver 校验并上传。禁止人工
   `docker load`、裸上传、现场构建或绕过 manifest。内部 Registry 建成并通过预生产演练后，退出
   离线通道，恢复 RepoDigest 提升路径。
4. `release-gate` 绑定 VERSION、commit、Alembic head、OpenAPI SHA256、SBOM、workflow run、
   四个 image ID、archive 摘要/大小、离线索引及 attestation；Ed25519 私钥不得进入仓库或生产，
   生产只安装固定路径的公钥与 key ID。
5. 首次空主机由独立 `release bootstrap --confirm-empty-host` 完成；普通更新才执行
   `release prepare`、`release activate`、`release status`。临时离线更新默认仅允许无迁移四镜像
   整包；已批准例外仅包括 `0080_security_daily_delivery_generation`→
   `0081_sign_adoption_contract`、`0081_sign_adoption_contract`→
   `0082_outbox_realtime_report_queue` 与 `0082_outbox_realtime_report_queue`→
   `0084_auth_security_and_ad_freshness`（一次整包覆盖中间修订
   `0083_password_change_token_lease`）三个全四镜像 expand，其他离线迁移仍拒绝。无迁移候选若未变更数据镜像
   定义/固定基础镜像、初始化脚本、Compose 存储/拓扑、schema 或 Alembic，可在显式传入
   `--allow-offline-no-conditional-evidence` 时省略本候选专属的数据镜像、备份恢复证据和同包
   预生产。上述 expand 仍必须提供并绑定 `data_images`，显式风险参数只允许省略
   `backup_restore_change`；每日备份、数据卷、迁移头、签名、四镜像完整性、健康、账本检查与
   失败补偿不得省略。

离线包上传失败、验签失败、导入失败或发布失败时必须保留 staging、release 状态和已导入镜像
供审计，禁止无范围 `prune`。执行前记录 manifest SHA-256；Phase 0 只有一名管理员时允许同一
具名操作者执行并自复核，但必须如实记录为单人变更，不得伪造第二身份。维护窗口不省略。

管理员初始化、正式厂商 Key 安装/轮换、测试号码管理和真实联调激活都是独立操作，绝不
夹带进代码发布。数据库、Docker volume、运行态目录和真实联调数据默认永久保留。
