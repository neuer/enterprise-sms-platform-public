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
精确 push CI 成功且用户已授权合并后，可将 PR 改为 Ready 并请求 squash merge。
项目流程不再额外要求独立 Code Review 或逐提交的口头评审确认；评审要求以 GitHub
实际生效的分支保护和 ruleset 为准，已配置的 required reviews 仍须满足。
required `ci-gate`、会话解决和冲突保护全部满足后才能合并，禁止管理员绕过。
若 PR 落后于 main 或存在冲突，先在任务分支处理并重新推送，再核验新 head SHA 的 CI
与 GitHub 实际合并条件；不因新提交重复索要口头评审确认。
合并后核对 PR 的实际 merge SHA 与 `origin/main`，并验证该 SHA 的 GitHub Actions
`ci-gate=success`；PR head 的绿色结果不能直接当作合并提交通过。main 的每次 push
均独立完整执行，不取消运行中或排队中的旧提交，也不以 PR 证据复用代替实际执行。
分支 push/fork PR 仅保留同一分支的最新一次；人工与定时运行使用独立并发组。
若合并提交没有 CI，使用现有 `workflow_dispatch` 执行完整 CI，并确认运行绑定目标
SHA。旧 `post_merge_*` 参数与 PR 证据复用入口已退役，人工运行不接受免检参数。
清理已合并分支前检查远端 ref，存在后续提交时保留；合并本身不代表测试部署完成。

CI 每次独立运行检查，不读取本地免检回执。门禁/前端契约由完整后端清单执行；
前端专用变更在 changes 中补跑 Python 契约。CI 控制入口变更覆盖全部相关 job；
README、CONTRIBUTING 及普通 Markdown 文档只执行规格/不变量检查并返回 `ci-gate`，
不能用 workflow 级 paths-ignore 跳过 required check。受保护运行手册、产品契约和未知
源代码路径仍保留相应校验；main、定时及人工运行一律全检。
后端单元测试拆为 5 片，隔离 PostgreSQL/Redis 按模块拆为 4 片，在独立 Ubuntu runner
并行执行。64 MiB 单字节分片用例与真实租约等待模块各占独立 runner，其余用例自动分配；
每片上传同一 SHA 的实际执行清单、JUnit 和覆盖数据。隔离分区自动收集 `tests/integration/` 以及显式真实 Redis 用例，
任何 skip 都失败；各集成模块从同一迁移基线克隆独立临时库，避免状态污染。
汇总重新独立收集完整清单，逐片验证确定性分配、分区互斥且穷尽、关键 marker 实际通过，
并核验 app 文件全集及 application/auth/pipeline/export/tasks/api 六项覆盖率，
同时保留原 full G2 的 services ≥80% 基线。
前端 CI 使用 `npm run test:coverage`（Python 3.12 / Node 24），先用真实 Vitest
负向 fixture 验证未导入文件进入分母，再执行完整组件测试。
`check_frontend_coverage.py verify` 重新核验完整 TS/Vue 文件集、测试执行清单、
报告哈希、源码/配置摘要和提交 SHA；API、会话、composables 分组独立达标，
不能用全局覆盖率抵消安全模块缺口。版本化门槛见该脚本和 Vite 配置。
证据包含 JSON summary、测试报告、manifest 与 lcov；失败重跑先废止旧成功回执。
Python 同一分片同时覆盖 `app`、`scripts`、`deploy/scripts`，后两组及高风险入口
有独立语句/分支门槛，合并前校验每个分片的覆盖数据摘要。完整分母、实测基线、
最终命令与语言边界见 [覆盖证据说明](docs/coverage-evidence.md)。
full G2 复用 `scripts/run_backend_tests.sh full <临时证据目录>` 和相同静态入口，
不再重复执行关键 marker 子集。普通分区仅保留公开快照缺失文档与本机无 Lua 的明确例外。

CI 按风险运行组件门禁；认证/授权/审计、加密与 PII、发送/厂商、迁移、部署和控制面等
受保护变更进入 G2 integration。21 项 API 验收按审批、心跳和其余场景分到 3 个独立
Mock 栈，场景集合互斥且穷尽，真实超时与全部断言不变；本地 full G2 仍串行跑全量。
发布控制烟测仍按路径选择，与 API 验收在独立 runner 并行，两者均须通过精确 SHA 校验。性能压测不进入 PR、push、
每日定时或人工 CI/G2 门禁，只在隔离测试环境按 `docs/PERFORMANCE.md` 专项执行并单独归档证据。

## 数据库迁移期限

Alembic 的在线执行和离线 SQL 都在迁移事务开始时设置 `lock_timeout=5s`、
`statement_timeout=30s`，不依赖某个历史 revision 才启用。前者限制每次锁等待，
后者限制单条 SQL；它们不是完整迁移链的总运行时限。迁移仍使用单个事务，超时或
历史数据约束校验失败时回滚并停止交付。应先诊断阻塞事务、语句成本与历史数据来源，
再按原维护入口重试；不得通过去掉期限或约束、改写审计事实、跳过 revision 强行成功。
已经发布的迁移保持不变；升级前应结合目标版本及数据规模安排维护窗口。

`0128_runtime_event_retention` 开始压缩观测明细，旧版指标读取器无法读取压缩后的历史
累计值。Registry 与离线清单均拒绝自动跨越此读取兼容边界，包括跨多个 revision、
反向迁移和加载跨界的历史发布清单；不可比较的变更端点也拒绝。不能将它声明为普通
`expand`，也未新增冷切换授权。边界两侧各自的正常发布及 0128 后同 head 的新候选仍按
现有门禁执行；forward rollback 继续保留当前 schema，拒绝直接恢复旧镜像，并绑定新
候选的 schema 证据。跨界交付需要另行设计和批准兼容流程，本轮合并不授权部署。
普通 `test_update apply` 也因现有 expand-only 检查拒绝 0128 中的压缩删除函数；
不放宽该检查，跨界更新须另有兼容与维护方案。

`0139_retire_vendor_test_tables` 删除已下线真实联调的五张专用表及其数据，
恢复密文字段闭集从 17 项变为 16 项。Registry 与离线清单同样拒绝自动跨越
0139（包括反向及跳过多个 revision），不能声明为普通 expand；downgrade 不创建
空表冒充历史数据恢复。旧备份应使用与其 schema 匹配的镜像和探针，跨界迁移须另有
经批准的维护方案。本次代码收尾不执行共享测试或生产数据删除。

## 按需测试部署

本节是 AGENTS 规则 39“开发与测试部署解耦”的权威合同。共享测试环境只跑 Mock，不保存
正式厂商凭据，也不保留需要迁移的数据（D126），因此发布方式是按精确 commit 铲掉重建：

- 只对 `ci-gate` 已成功的精确 commit 重建，默认是合并后的 `origin/main`；分支重建只作为
  明确的合并前验收例外。
- 每次重建都会销毁测试环境的 PostgreSQL、Redis 与 Mock 目录卷，再构建、迁移、完成写者
  切换并执行 `seed-dev`。测试环境里不得存放任何需要保留的数据。
- 服务器残留旧联调标记或 `.env` 不是纯 Mock 时入口失败关闭；由操作者手工清除残留标记
  与正式凭据后再重建，不得绕过。
- 只有入口输出 `state=rebuilt` 且完成对应表面验收才算成功。生产发布不走本入口。

测试部署与日常开发解耦。只有需要共享环境、浏览器或接口验收的功能才部署；纯文档、
纯测试和不改变行为的重构无需部署。默认等待 PR 合并后执行：

```bash
scripts/test_rebuild.sh --ref origin/main
```

入口依次完成：解析并核验目标 commit 的 `ci-gate`、在服务器检出该 commit、调用
`scripts/local_test.sh reset` 重建全栈、写入 GitHub `test` Environment 的 Deployment 记录。
`--dry-run` 只做本地解析与 CI 核验，不连接服务器。

首次使用时才准备本地连接文件和 GitHub CLI：

```bash
cp test-update.env.example .env.test-update
chmod 600 .env.test-update
gh auth status --hostname github.com
```

`.env.test-update` 只允许 `SMS_TEST_UPDATE_TARGET` 与 `SMS_TEST_UPDATE_PORT`，被 Git
忽略且必须为当前用户拥有的普通文件。不得在仓库、命令历史或文档中记录真实主机地址、
账号、密钥或测试数据。鉴权失效时只用 `gh auth login --hostname github.com --web`；
不得粘贴或长期导出 GitHub token。

PR 合并只表示仓库集成完成，不代表测试服务器已更新。

## 生产发布

历史 `0080–0116` 离线迁移例外及 `cold_cutover` 入口已退役。现役生产与共享测试按最新维护基线支持，
不再生成、激活、续跑或回退这些历史升级包；`release status` 继续只读展示既有状态，历史事件日志继续保留。
保留 Alembic 迁移链、发布证据和运行数据；当前发布失败仍按现役恢复合同处理。

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
   整包，版本间离线迁移一律拒绝。无迁移候选若未变更数据镜像
   定义/固定基础镜像、初始化脚本、Compose 存储/拓扑、schema 或 Alembic，可在显式传入
   `--allow-offline-no-conditional-evidence` 时省略本候选专属的数据镜像、备份恢复证据和同包
   预生产。每日备份、数据卷、迁移头、签名、四镜像完整性、健康、账本检查与失败补偿不得省略。

离线包上传失败、验签失败、导入失败或发布失败时必须保留 staging、release 状态和已导入镜像
供审计，禁止无范围 `prune`。执行前记录 manifest SHA-256；Phase 0 只有一名管理员时允许同一
具名操作者执行并自复核，但必须如实记录为单人变更，不得伪造第二身份。维护窗口不省略。

管理员初始化与正式厂商 Key 安装/轮换都是独立操作，绝不夹带进代码发布。数据库、
Docker volume 与运行态目录默认永久保留。
