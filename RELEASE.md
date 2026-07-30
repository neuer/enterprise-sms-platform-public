# 企业短信管理平台 v1.6 发布说明

> 本文件保留 2026-07 建设期候选的历史发布记录。当前日常流程以 `MAINTENANCE.md`
> 为准，当前阻塞以 `PROGRESS.md` 为准。

- 发布代码基线：2026-07-15 安全与前后端契约对齐、统一发布控制及远端 Mock 演练已合并并推送 `main`，当前仓库候选为 `<redacted-commit>`；未创建正式发布 tag
- 最近完整 G2：2026-07-15（Asia/Shanghai），候选 `<redacted-commit>` 的 GitHub Actions run [`<redacted-run>`](https://github.com/example/enterprise-sms-platform/actions/runs/<redacted-run>) 退出 0
- 最近四镜像发布门禁：相同运行代码基线已保留 API、Web、PostgreSQL、Redis 均为 0 HIGH / 0 CRITICAL 的本地扫描证据；候选 `<redacted-commit>` 的 hosted Release Gate run [`<redacted-run>`](https://github.com/example/enterprise-sms-platform/actions/runs/<redacted-run>) 退出 0。P0 合并后的最终不可变证据归档到生产变更单与 release manifest，不再通过额外文档提交回填 SHA/run ID
- 交付状态：仓库内安全、契约、G2、基础镜像 HIGH/CRITICAL 阻塞与远端 Mock 发布演练均已闭环；剩余外部系统和真人验收事项见 [HANDOVER.md](HANDOVER.md)

## v1.6.15 安全与前后端契约对齐候选

- 认证与授权：浏览器令牌改为受限生命周期；用户会话版本在事务内更新；批次取消、改期和失败重发按后端角色矩阵强制校验，前端路由与接口契约同步。
- 数据与输入安全：持久化文本投影统一手机号脱敏；XLSX 导入在解析前限制压缩包资源；审计、导入、导出和回调边界继续遵守无明文手机号与无凭据输出规则。
- 出站与部署：厂商、企微、SMTP 目的地增加生产级 allowlist/HTTPS 约束并禁用环境代理继承；Compose 按进程最小化 secrets 挂载；发布扫描改为只读镜像归档且不向 Trivy 暴露 Docker socket。
- 数据库与契约：安全加固迁移兼容已有数据库；模板角色、应用频控覆盖等实现与 `openapi.yaml` 回填一致；迁移与 69 个 OpenAPI operation 零差。
- GitHub G2（run `<redacted-run>`）：1128 项后端测试、services 86%、SEC-01–07、UAT 20/20、Node 24 前端 22 文件/84 项全部通过；acceptance 1800 次 P95 `90.30ms`，verify 60 次 P95 `605.52ms`，bulk 180 次，排空 `66.27s`。
- 发布镜像：代码基线 `<redacted-id>` 的本地 fail-closed 门禁扫描四个最终交付镜像，API/Web/PostgreSQL/Redis 均为 0 HIGH / 0 CRITICAL，未降低阈值、未忽略修复延期项、未引入运行时外部 CDN。
- 远端演练：修复提交 `<redacted-id>` 在隔离主机完整通过 Web/API-only、数据镜像、失败补偿与 TERM/resume；公网边界和管理员登录/退出浏览器冒烟通过。首次执行导致 Mock 卷重置的事件、恢复和防复发证据保存在受限归档，不随公开快照发布。
- 候选冻结：`<redacted-commit>` 的 hosted CI/Release Gate 已成功；P0 只修正交付台账，合并后的最终 `headSha`、workflow run、发布证据 `candidate_commit`、release ID 与四镜像 RepoDigest 统一绑定到生产变更单和后续 release manifest，不再以回填文档的方式改变候选。
- 生产边界：尚未完成外部 TLS/HSTS、真实 LDAP/厂商/告警渠道、生产 18 件 secrets、受控仓库 RepoDigest、24 小时十万条压测、主备 RTO、真人 28 例 UAT 与生产变更单，当前不得创建正式 `v*` tag 或上线。

## v1.6.14 安全基础镜像

- API 使用 digest 固定的 Python 3.12 Alpine 多阶段镜像；Web 使用 Node 24 Alpine 构建和升级安全包后的稳定 Nginx Alpine；PostgreSQL 16/Redis 7 使用 digest 固定的最终内部镜像。
- PostgreSQL 最终镜像以扁平化层移除官方历史中的旧 Go `gosu`，保留官方入口、PostgreSQL 16、数据目录和停止信号；`su-exec` 兼容替代已通过 `sms_app` 初始化、权限和数据卷重启验证。Redis AOF 重启持久化同步通过。
- 干净候选 `<redacted-commit>` 的 Trivy 0.70.0 结果为 API 0、Web 0、PostgreSQL 0、Redis 0，均为 0 HIGH / 0 CRITICAL，门禁退出 0。镜像 ID：API `<redacted-digest>`、Web `<redacted-digest>`、PostgreSQL `<redacted-digest>`、Redis `<redacted-digest>`。
- 安全镜像变更后的完整 G2 在 `<redacted-id>` 退出 0：后端 644 项、services 86.01%、迁移与 69-operation 契约、SEC-01–07、UAT 20/20、Node 24 前端 53 项全绿；受理 P95 20.25ms、verify P95 121.38ms、排空 61.40s，测试卷已清理。
- 生产仍需完成外部 TLS/HSTS、真实 AD/厂商/告警、生产 18 件 secrets、24 小时十万条压测、主备 RTO 和真人 UAT；仓库门禁全绿不等于批准上线。

> M4.1 页面对齐补丁已完成全新 Mock 卷 G2、四角色真实浏览器与 390px 验收；发布时应以包含 M4.1 最终交付提交的候选分支为准，不再使用未含补丁的旧 `m4` 镜像。

## v1.6.13 生产就绪依赖与 warning 硬化

- pytest 默认 warning 策略改为 `error`；仅保留 FastAPI/Starlette 的 1 条 `StarletteDeprecationWarning` 与 ldap3/pyasn1 的 2 条 `DeprecationWarning`，每条均按完整消息、具体类别和具体模块精确匹配，禁止宽泛忽略。
- 范围内刷新确认 FastAPI 0.139.0、Starlette 1.3.1、ldap3 2.9.1、pyasn1 0.6.4 已是当前解析结果；升级 `pytest-asyncio` 0.26.0 → 1.4.0，消除其 teardown 创建但不关闭 event loop/socket 的资源 warning。
- Trivy 真实扫描发现并推动 `cryptography` 45.0.7 → 48.0.1，关闭 CVE-2026-26007 与 GHSA-537c-gmf6-5ccf；AES-GCM/HMAC keyring 定向回归 23 项通过。
- 后端最终完整测试 `638 passed` 且无 warning summary，`pip-audit --strict` 返回 `No known vulnerabilities found`。容器基础镜像扫描结果与剩余阻塞在本轮最终发布验收记录中单独列明，不以 Python 依赖审计替代镜像扫描。

## v1.6.13 生产发布验收结论

- 候选分支 `codex/release-readiness-hardening`，真实四镜像发布门禁候选 commit `<redacted-commit>`；完整隔离 G2 代码基线为 `<redacted-id>`。未合并 main、未创建 tag。
- 完整隔离 G2 退出 0：637 项后端测试、services 86.01%、69-operation 契约、SEC-01–07、UAT 20/20、Node 24 下 20 文件/53 项前端测试与构建全绿。性能为 acceptance P95 19.1ms、verify P95 129.7ms、bulk 180 次、排空 61.27s。
- 1280px 与 390px 真实浏览器检查覆盖六个核心路由，无文档/筛选卡内部横溢出、无控制台 warning/error，ops tabs 的有意横滚策略保留；辅助文本三个背景对比度均≥4.5:1。390px 下 nav/button/input/date/tab/pagination 的实际代表控件高度均为 44px；实测浏览器为 fine pointer，coarse pointer 由同一 44px 媒体规则契约覆盖。
- 内部 Nginx 提供计划规定的 CSP 与 Permissions-Policy，不发 HSTS；HSTS/HTTPS 跳转是外部 TLS 终结器的发布证据。
- **当前不可发布**：`bash scripts/verify_release.sh` 在干净候选 `<redacted-id>` 成功构建 API/Web，用 Trivy 0.70.0 扫完四镜像并输出镜像标识：API 34 HIGH/2 CRITICAL（Python 依赖 target 为 0）、Web 35/2、PostgreSQL 48/9、Redis 16/3，最终退出 1。不得降低阈值或忽略未修复项；必须先审批基础镜像策略并将四镜像扫描跑至退出 0。
- 完整逐项证据见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)。D01 决策未变更，本轮未修改 `TASKS.md`。

## v1.6.12 全库审查硬化

- 可靠性：修复 scheduled 审批提前入队/配额回补、非法 batch chunk 领取、Send 结果未知与陈旧 submitting 恢复、明确失败终态聚合，以及持久化重试预算、到期时间和 fenced 补偿。
- 密钥轮换与幂等：报告、发送、导入在 HMAC 轮换期兼容历史候选但只持久化 active 值；幂等 claim/续租/释放位于全部 Redis、频控、配额和发布副作用之前。
- 网络安全：LDAP real 强制证书验证；可信代理固定；callback 在保存与出站前双检，连接固定批准地址并受地址切换、墙钟超时和 RFC1918/ULA 私网 CIDR 限制。
- 重放与迁移：报告 callback 使用规范化不可变事件身份；0012 对旧引用歧义 fail-closed，成功升级/降级和清理竞态有真实 PostgreSQL 证据。
- 运行配置：类型化策略覆盖完整 sys_config；Admin 以全局事务锁和完整行快照做最终校验；登录、callback、导入、重新审批、测试发送、uncertain 与五个启动调度键按明确边界生效；Celery 跨事件循环加载使用一次性 NullPool。
- 权限：approver/admin 审批列表为全量 scope；operator/viewer 没有扩大。

对应提交和逐项证据见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)“2026-07-13 全库审查十二项闭环”。数据基准为 schema v1.6.12、Alembic head `0013_auth_runtime_config`，OpenAPI 保持 69 个 operation。

## 已实现需求

- 分类、发送与可靠性：FR-00/00a、FR-01–05a。包括单点类别策略/计费、API 与 Web 发送、加密号码导入、定时/审批、双队列令牌桶、厂商错误矩阵、余额熔断、uncertain 禁重发与受控 reconcile、失败号码新批次重发。
- 报告、回复与回调：FR-06、FR-07/07a。GetReport/GetReply 先加密落 raw，支持重放与 unmatched；上行回复/退订；回调 HMAC、CIDR 双检、引用式任务、五次重试/dead/手动重推。
- 内容与号码治理：FR-08–11a。批内去重、黑名单、Aho-Corasick 敏感词、应用/部门配额、受理限流和号码级频控均按唯一服务实现。
- 模板、签名与告警：FR-12–15、FR-25、FR-27。支持 `{n}`→`{sN}`、厂商同步、统一 alert_log/企微/SMTP、余额/失败/异常告警、双条件 anomaly、全部 beat 任务追踪和 API 进程心跳巡检。
- 查询、报表与管理：FR-16–20、FR-26。批次/号码/时间线、授权解密与加密导出、唯一成功率口径、统计仪表盘、应用双 Key、用户角色覆盖/强制下线、系统参数和只增审计。
- 数据安全与生命周期：FR-21、FR-22/22a。手机号 enc/hmac/mask/key_version、AES-GCM/HMAC keyring、verify OTP 等长打码、月分区、raw/import/export/audit 生命周期和 owner/app 权限隔离。
- 非功能：NFR-01–06。Docker Compose 生产契约、Python 3.12/PostgreSQL 16/Redis 7/Node 24、冷备脚本/手册、Prometheus 指标、结构化无 PII 日志、完整契约与安全验收。

需求到任务、实现、接口和 UAT 的逐项映射见 [docs/TRACEABILITY.md](docs/TRACEABILITY.md)。

## 发布验证

- v1.6.12 审查硬化 G2：使用 `API_PORT=18100 MOCK_VENDOR_PORT=19128 WEB_PORT=18180 bash scripts/verify_all.sh`，从 `down -v` 开始并退出 0，测试卷自动清理。
- v1.6.12 后端：630 项 pytest、services 覆盖率 86.01%；Ruff 全库、Mypy 158 个源文件、硬规则、规格、69-operation 契约通过。
- v1.6.12 数据：真实 PostgreSQL schema/Alembic 双建库一致；0012/0013 legacy 默认插入、自定义值保留、歧义拒绝与 downgrade 通过；配置并发最终组合合法，callback/reconcile 在同进程连续跨事件循环执行通过。
- v1.6.12 安全与 UAT：SEC-01–07、固定 API UAT 20/20 全绿并完成 rollback；UAT 临时营销窗口在用例结束即恢复，性能前置 workload 为空。
- v1.6.12 性能：1800 次 API 受理 P95 20.3ms；60 次 verify 端到端 P95 120.7ms；180 次 bulk；停止施压后 60.59s 排空。
- v1.6.12 前端：干净 Node 24 容器 19 个测试文件/49 项测试、类型检查与生产构建通过。宿主机既有 node_modules 状态不作为产品限制或发布证据。
- Node 24 升级回归（2026-07-13）：`node:24-alpine` 实际版本 v24.18.0/npm 11.16.0；生产依赖审计 0 漏洞，类型检查、19 个测试文件/49 项测试、Vite 生产构建、真实多阶段镜像构建和外部 CDN 检查全绿；未改动第三方依赖版本或 integrity。
- M4.1 G2：以 `API_PORT=18100 MOCK_VENDOR_PORT=19128 WEB_PORT=18180 bash scripts/verify_all.sh` 避开其他项目占用端口，从 `down -v` 开始全新卷执行并退出 0；端口覆盖是门禁公开参数，不改变被测内容。
- M4.1 浏览器：管理员覆盖 PRD 18 页面形态（批次详情为右侧 drawer），viewer/operator/approver 菜单与直达守卫符合后端角色矩阵；16 个落地路由在 1280px 与 390px 均无整页溢出、无错误提示、控制台零错误。
- M4.1 后端：Ruff、Mypy 153 个源文件、504 项 pytest、services 覆盖率 84.90%；迁移双空库、69 个 OpenAPI operation、SEC-01–07、UAT 20/20 全绿。
- M4.1 性能：1800 次 API 受理 P95 20.8ms；60 次 verify 端到端 P95 121.1ms；180 次 bulk；停止后 60.08s 排空。
- M4.1 前端：Node 20 生产构建、类型检查、19 个测试文件/49 项测试通过；主 JS 416.18kB、主 CSS 203.63kB，业务页面按路由拆包。

- M4 G1：`bash scripts/verify_milestone.sh M4` 退出 0，并创建 tag `m4`。
- G2：从 `docker compose down -v` 开始执行 `bash scripts/verify_all.sh`，退出 0 且自动清理测试卷。
- 后端：Ruff、Mypy 153 个源文件、497 项 pytest、services 覆盖率 84.86%。
- 数据/契约：Alembic 与 `schema.sql` 双空库表/列/索引/约束一致；69 个 OpenAPI operation 零差。
- 安全与 UAT：SEC-01–07；固定 20 项 API UAT 全部成功并完成 rollback。
- 性能：1800 次 API 受理 P95 19.7ms；60 次 verify 端到端 P95 120.9ms；180 次 bulk；停止施压后 60.61s 排空。
- 前端：Node.js 20 生产构建、`vue-tsc --noEmit`、13 个测试文件/25 项组件测试通过；字体资源自包含。

## BLOCKED 与已知限制

- D01：asyncpg 不能单次执行多语句 `schema.sql`；已采用同事务无损切分执行的安全降级，并由重组测试、真实迁移和双空库结构门禁保护。详见 [docs/DECISIONS.md](docs/DECISIONS.md)。D01 不是待重试故障；当前活跃发布 BLOCKED 为基础镜像 HIGH/CRITICAL 扫描失败。
- 自动化使用 vendor/auth mock 与告警 log-sink，不构成真实 LDAP、厂商、企微/SMTP 的生产连通性证明。
- 真实 AD UAT 01–04、真人完整 28 例、24 小时 10 万条 Locust、真实主备 RTO≤30min 演练、厂商主备出口/QPS 与生产 18 件 secrets 均为发布前人工事项。
- M4.1 已将业务页面和 Element Plus 改为按需加载；候选构建主 JS 416.18kB、主 CSS 203.63kB。ECharts Canvas 494.53kB 仅随图表页面异步加载，继续按首月真实加载指标观察。

## 部署与回退

生产部署按 [deploy/README.md](deploy/README.md)，业务拉取权切换按 [PRD.md 第 10 章](PRD.md)，密钥、DBA、备份和冷备分别按 `deploy/secrets.md`、`deploy/dba.md`、`deploy/backup-restore.md`、`deploy/failover.md`。任一关键验证失败应停止上线，并按 [HANDOVER.md](HANDOVER.md) 的单应用或整体回退顺序执行。
