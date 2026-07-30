# BOOTSTRAP.md — 从零建设开工引导

本文件保留从空仓库到完整平台的建设流程。平台进入维护期后，日常开发直接使用
`MAINTENANCE.md`，不重走 M-1、里程碑或初始化步骤。

## 1. 阅读顺序（动手前必须读完）

1. `AUTOPILOT.md` + `PROGRESS.md` — 无人值守执行、门禁、失败与续跑
2. `CLAUDE.md`（Codex 同时遵守 AGENTS.md）— 工程硬性规则
3. `PRD.md` — 需求与验收口径（冲突时以它为准）
4. `docs/DECISIONS.md` + `docs/TRACEABILITY.md` — 已确认决策与需求追踪
5. `schema.sql` — 数据模型（已定稿，改动走 Alembic 且需在 PR 说明）
6. `openapi.yaml` — 平台 API 契约（实现须与之一致）
7. `docs/vendor-api.md` — 厂商精确报文与 mock 契约（适配层唯一依据）
8. `docs/UAT.md` + `docs/ui-design.md` + `docs/sms-ui-prototype.html` — 验收与 UI 基准
9. `TASKS.md` — 唯一执行清单（M0→M4 顺序推进）

## 2. 仓库基线与目标结构

初始仓库只含规格、契约与门禁骨架。你需要按 CLAUDE.md 目录约定创建 `backend/`、`frontend/`，并以 `deploy/docker-compose.yml` 为部署契约（既有服务名、队列名、secrets 名不得更名；允许按文档增加 `migrate` 等明确约定的服务）。

目标模式的第一阶段是 **M-1 仓库预检**：

```bash
git rev-parse --is-inside-work-tree
git status --short
git check-ignore .env deploy/secrets/dev-apikeys.txt
bash -n scripts/verify_all.sh scripts/verify_milestone.sh
python3 scripts/check_spec_consistency.py
```

任一命令失败都先修复仓库基线，不得进入 M0。`.env`、`deploy/secrets/`、导入/导出/备份产物必须被 `.gitignore` 排除。

## 3. 主机依赖与本地 secrets（M0 前置）

完整平台开发完成后，日常本地测试优先使用 [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md) 的一键脚本与登录说明；以下手工步骤保留为底层初始化参考。

### 手机远程 Mac / 无桌面会话

通过手机 ChatGPT Remote 控制 Mac 开发时，先运行：

```bash
scripts/docker_public.sh doctor
```

开发、测试、快速更新及本地发布候选入口会自动创建一次性空认证 Docker 配置，保留
OrbStack 的 Unix Socket、Compose 和 Buildx，但不读取 `macOS Keychain`。成功、失败或
信号退出都会删除临时配置；自动化不得删除全局 Docker 配置，也不得解锁 macOS
Keychain。

该通道只用于 Docker Hub/GHCR 等公开镜像和本地镜像操作。私有仓库登录、推送及最终
production RepoDigest 回拉必须转到受控 CI 或专用发布构建机，不能把 registry 凭据
交给手机远程代理。

目标模式主机需要 Git、Bash、Docker Engine + Compose v2、Python 3.12、Node.js 24/npm、curl 和 OpenSSL。先执行：

```bash
git --version
docker compose version
python3 --version
node --version
npm --version
```

随后只创建本地配置和随机 secrets；**此时不要启动 Compose**，因为 backend/frontend 骨架由 T0.1～T0.3 创建。不要在文档或命令中写固定开发口令：

```bash
scripts/local_test.sh prepare
```

该命令只补齐缺失文件，已有安全随机值不会覆盖，所有文件保持 0600 且不会在终端回显。开发期 `.env` 必须为 `DEBUG=1`、`AUTH_MOCK=1`、`VENDOR_MOCK=1`，且 `VENDOR_BASE_URL=http://mock-vendor:9028`；任何自动化测试不得请求真实 LDAP、厂商、企微或 SMTP。

完成 T0.1～T0.5 后首次启动：

```bash
docker compose -f deploy/docker-compose.yml --profile dev up -d --build
docker compose -f deploy/docker-compose.yml exec -T api python -m app.cli seed-dev
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U sms_owner -d sms -v ON_ERROR_STOP=1 < deploy/seed.example.sql
```

不得把 `postgresql+asyncpg://` 连接串直接传给 `psql`；所有迁移和 seed SQL 均通过 `migrate` 服务执行。

## 4. 首个本地管理员（避免鸡生蛋）

- 完成数据库迁移后，在空系统的真实交互 TTY 执行 `sudo /usr/local/sbin/sms-compose init-admin --show-temporary-password`；默认用户名为 `admin`，也可传 `--username` 和 `--display-name`
- 命令生成 20 位临时密码，事务提交后仅在当前 TTY 显示一次；Codex 可通过 PTY 代执行并把当次输出转告操作者。首次登录必须修改密码
- 初始化只使用内置本地认证源，与 AD/LDAP 完全无关。登录后再到系统配置页保存、测试并启用 AD；不得使用环境变量名单或其他隐藏方式提权

## 5. 开发循环（每个任务）

1. 读 TASKS.md 当前任务与验收标准
2. 实现（含单测）→ 当前已存在测试全绿
3. 跑该任务"验收"命令/场景
4. 勾选 TASKS.md 复选框
5. `git commit -m "feat(M1): T1.7 双队列worker与uncertain机制"`（格式见 CLAUDE.md）

里程碑完成定义（DoD）：该里程碑全部任务勾选 + 验收断言全过 + `bash scripts/verify_milestone.sh Mx` 全绿 + 无 CLAUDE.md 硬性规则违反。完整 `verify_all.sh` 只在 G2 运行。

## 6. GitHub CI

`.github/workflows/ci.yml` 接收所有分支 push、指向 `main` 的 PR、人工
`workflow_dispatch` 和每日定时任务。仓库 owner 的同仓分支只由 `push` 执行一次 CI，
其重复 `pull_request` 事件明确跳过；fork 没有同仓 push，才由 `pull_request` 执行。分支
push 始终按相对 `main` 的完整差异分类，因此 Actions 自动创建 Draft PR 不依赖递归事件。
owner 同仓分支的精确 push CI 成功后，独立 `workflow_run` 只把 head SHA 完全一致的 PR
改为 Ready 并请求 auto squash merge；required checks、会话解决和冲突保护仍由 GitHub
执行，禁止 `--admin` 绕过。
PR 与 `main` push 按变更文件选择检查；G2 只由高风险变更、人工/定时事件或失败关闭回退
触发。稳定 job 名称及职责为：

- `changes`：始终执行规格一致性、硬规则静态检查和路径分类；
- `backend`：按需执行 Ruff、Mypy、后端测试与覆盖率、迁移和 OpenAPI 契约；
- `frontend`：按需在 Node 24 下安装、构建、类型检查和组件测试；
- `security`：安全关键路径、依赖锁、镜像或发布控制变更时执行 Bandit，以及 Trivy
  依赖、许可证、secret 与配置扫描；
- `g2`：迁移/部署、认证/审计、加密与 PII、发送/厂商链路、CI/门禁等高风险 PR
  执行权威 `scripts/verify_all.sh --mode integration` 的运行态阶段；普通后端业务 PR
  不进入该阻塞链路；
- `ci-gate`：始终汇总本次预期与实际 job 结果，预期 job 缺失、跳过或失败时失败关闭。

普通 `docs/plans/**` 与 `docs/TEST-REPORT-*` 只运行秒级 `changes` 和 `ci-gate`；纯前端、
纯后端测试、普通后端业务或契约文档只增加对应快速 job。混合变更取规则并集，未知路径保守
全跑；Git 差异无法可靠取得或分类器异常时不得伪装成功。可靠 `main` push 若 tree 与已
合并 PR head 完全一致，会复用该 PR 的精确 `ci-gate`，只运行 `changes` 与 `ci-gate`，
跳过 backend/frontend/security/g2，不重复运行 G2 或快速组件检查；证据不可复用时按路径
重新分类执行。未知路径、空差异
或事件元数据异常仍强制全跑。人工
`workflow_dispatch` 与每天北京时间 02:17 强制运行 backend、frontend、security 和完整
G2，为分类规则长期漂移提供兜底证据。

PR 的高风险 G2 使用 `scripts/verify_all.sh --mode integration`，复用同一 11 阶段实现，
但跳过已由快速 job 覆盖的静态阶段；性能和 release-control 只在相应路径变化时加入。
人工、定时与发布候选继续运行未裁剪的完整模式。可靠 `main` push 若能证明其 tree 与
已合并 PR head 完全一致，复用该 PR 的精确 `ci-gate` 证据，不重复计算重型 job；任何证据
缺失或来源不精确都失败关闭为重新运行。

本地 `bash scripts/verify_all.sh` 保留为专项完整复验入口，日常开发使用
`scripts/dev_check.sh --changed`。

干净 Runner 先执行 `scripts/local_test.sh prepare`，只生成开发 `.env` 和临时 mock secrets；工作流不得读取或配置生产 secrets，也不得上传开发密钥或可能包含手机号的运行产物。

公开主仓库保留 PR、会话解决、线性历史及禁止 force-push/delete 的保护。`ci-gate` 是
`main` 唯一 required check，并绑定 GitHub Actions 应用；不得把 `backend`、`frontend`、
`security` 或 `g2` 单独设为 required。普通无迁移测试更新允许 CI 并行运行；high-risk、
迁移或控制面更新必须在 `apply` 前验证目标 commit 的精确 `ci-gate=success`。私有归档仓库
只保留 Issue 与历史，不承担托管 CI。

`.github/workflows/release-gate.yml` 的 `Release Gate` 只允许人工或 `v*` 标签触发；
最终 SHA 必须重新执行完整质量、安全与 G2 门禁，再构建并扫描四个镜像。成功证据绑定
VERSION、commit、Alembic head、OpenAPI SHA256、四份 CycloneDX SBOM、workflow run 与
镜像身份，并通过 GitHub attestation 和不可变 artifact 归档；任何绑定不一致都禁止生成
production manifest。它不作为日常 PR required check。

## 7. 决策与阻塞处理

- 产品决策缺口：先查 PRD.md；仍无 → 采取最保守实现，在代码注释 + commit message 写明 `ASSUMPTION:`，**继续推进不停待**
- 技术选型：CLAUDE.md 已锁定，不引入新框架/替代件
- 发现文档间冲突：以 PRD.md 为准，并在 `docs/DECISIONS.md` 追加一条记录

## 8. Kickoff Prompt

**目标模式（无人值守）请改用 AUTOPILOT.md 第 7 节的 Prompt。** 交互模式仍可用下面这条：

```
你在企业短信管理平台仓库工作。先读 BOOTSTRAP.md 并按其第 1 节顺序读完全部文档再动手。
随后从 TASKS.md 的 M0/T0.1 开始逐任务实施：每完成一个任务→跑验收→勾选复选框→按规范 commit。
全程遵守 CLAUDE.md 硬性规则（尤其 1/2/4/5/18/21/23/24）；默认 VENDOR_MOCK=1，任何测试不得请求真实厂商。
遇到 PRD 未覆盖的决策：按 BOOTSTRAP.md 第 7 节处理（写 ASSUMPTION 并继续，不停待、不静默改需求）。
现在开始执行 M0。
```

## 9. 上线前人工事项清单（非代码，运营方完成）

- [ ] **发布"停拉通知"**：T0 起所有直连系统禁止调用 GetReport/GetReply（拉走即消费，详见 PRD 第10章切换方案）；收口后请厂商重置密钥
- [ ] 向厂商报备**主、备两个出口 IP**（规避 1010）
- [ ] 与厂商确认真实 QPS 与单次号码上限 → 调 sys_config.vendor_qps / vendor_batch_size
- [ ] 按 `deploy/secrets.md` 将生产 18 件 secrets 分别落盘到主、备节点：vendor 2、
  data 2、JWT、LDAP、metrics、DB owner + 七职责密码、三域 Redis ACL 密码
- [ ] 先执行 `sms-compose init-admin --show-temporary-password` 并完成本地管理员首次改密；再在系统配置页保存、测试、启用 AD，配置 4 个安全组角色映射
- [ ] 企业微信机器人 webhook 与告警邮箱写入 sys_config
- [ ] 按 docs/UAT.md 完成 28 例真人验收，并把报告归档到受限生产变更单（不进入公开仓库）
- [ ] 上线首月：stat_daily 计费条 与 厂商账单 逐日核对
