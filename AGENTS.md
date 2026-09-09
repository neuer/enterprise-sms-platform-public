# AGENTS.md — 企业短信管理平台工程约定 v1.6

本文件是 Codex 与 Claude Code 共用的工程约定。日常开发与交付按
[MAINTENANCE.md](MAINTENANCE.md) 对应章节执行；涉及外部阻塞时查看
[PROGRESS.md](PROGRESS.md)。按当前任务读取所需材料，同一任务已读取且未变化的内容无需重复加载。

产品行为以 [PRD.md](PRD.md) 为准；接口、库表、厂商精确报文与 Mock 契约分别查
[openapi.yaml](openapi.yaml)、[schema.sql](schema.sql) 与 [docs/vendor-api.md](docs/vendor-api.md)。
平台错误语义见 [PRD 错误码表](PRD.md#platform-error-codes)；安全审查遵循 [SECURITY.md](SECURITY.md)。文档与实现冲突时记录具体差异，结合有效变更依据判断；
不能用过期文档自动降低安全要求。

## 技术栈（锁定，勿替换）

- 后端：Python 3.12 / FastAPI / SQLAlchemy 2.x (async) / Alembic / Celery 5 + Redis broker
- 前端：Vue 3 + Vite + TypeScript + Pinia + Element Plus + ECharts
- 存储：PostgreSQL 16（asyncpg）、Redis 7（队列/配额/限流/幂等/JWT黑名单/缓存）
- 部署：Docker Compose；python:3.12-alpine / node:24-alpine（构建）/ nginx:stable-alpine / postgres:16-alpine / redis:7-alpine；生产基础镜像固定 digest，四个最终镜像必须通过独立 Trivy 门禁；`deploy/docker-compose.yml` 的服务/队列/secrets 名不可改
- 关键库：ldap3、pyahocorasick、httpx、openpyxl、cryptography（AES-GCM）
- 可变业务参数读取 `sys_config`；运行凭据按规则 1 的文件边界与明确例外处理。

## 按任务读取

- 认证、浏览器会话与账号管理：[PRD 用户与角色](PRD.md#auth-contract)。
- 真实联调与 API UAT：[PRD FR-19a](PRD.md#vendor-uat-contract) 和 [受控联调手册](docs/runbooks/controlled-real-vendor-test.md)。
- 前端页面、组件与样式：[UI 设计规范](docs/ui-design.md)，共享实现入口见该文档第 8 节。
- 本机 Mock 启动：[本地测试](docs/LOCAL_TESTING.md)；共享测试更新与生产交付分别按 [维护入口](MAINTENANCE.md) 对应流程。
- 数据库权限或 Redis 部署：[数据库角色](deploy/database-roles.md) 与 [Redis 故障域](deploy/redis-ha.md)；仅加载涉及的文档。

## 硬性规则

1. **所有运行凭据默认只能通过 Docker secrets 文件挂载读取**：生产清单为厂商 SecretName/SecretKey、AES 数据密钥、HMAC 索引密钥、独立 API Key pepper keyring、JWT 密钥、LDAP bind 密码、DB owner 密码、auth/accept/send/callback/export/scheduler/metrics 七个独立数据库运行密码，以及 broker/auth/control 三个独立 Redis ACL 密码；禁止入环境变量明文、入日志或入普通 API 响应（callback_secret 仅允许 AES-GCM 密文入库）。安全日报是明确的产品例外：管理员可在 `/security-daily` 页面配置 Resend Key 和最多 3 个收件人，Key 允许以明文存入专用 `sys_config` 配置并由 API 同步到独立 mailer 的 `resend.json`；不得用于其他平台凭据，审计只记录 configured 状态和数量。另一个例外是管理员可在真实联调页面一次性输入厂商 SecretName/SecretKey：明文只可短暂存在于组件局部的**浏览器易失内存**，必须立即通过 WebCrypto 封装为仅 `vendor-control-agent` 可解密的密文；禁止任何浏览器持久化，禁止写入 Pinia、localStorage、sessionStorage、IndexedDB、Service Worker cache 或 URL，禁止进入普通 API 明文、数据库、队列、审计、日志、指标和错误详情，提交结束必须清空且不得回显值、长度、前缀、摘要或哈希。settings 读取后去除行尾换行，DB DSN 必须用 SQLAlchemy `URL.create` 组装，禁止字符串拼接导致转义或泄露
2. **手机号永不明文持久化**：逐号码记录必须经 `services/crypto.py` 生成 `phone_enc`(AES-256-GCM) / `phone_hmac`(HMAC-SHA256 hex) / `phone_mask` / `key_version`；精确查询一律走 phone_hmac；文件、JSONB、缓存与日志同样不得留明文。对外解密只允许"详情按角色查看"与"授权导出"；内部受控解密白名单为 raw 解析/重放、callback 投递、发送下发、UAT 定位，以及退订入黑名单（reply optout：内存解密后经 `protect_phone` 重加密入库，不回写明文）。严禁把明文再次写入任何持久层
3. 所有厂商 HTTP 必须经选定 adapter；当前生产 adapter 为 `vendor/zhihui.py`。业务代码不得直接 httpx 调厂商；统一超时 10s、连接池、结构化日志。仅当调用前确定性不可用，或协议明确拒绝且 `safe_to_failover=true` 时允许切换供应商；`uncertain`/`submitted` 后禁止自动切换任何供应商
4. **Send 超时/网络异常 = 结果未知**：chunk 置 `uncertain`，**严禁自动重发或自动换供应商**；由 reconcile 任务通过 raw_vendor_log.custom_ids 索引定位并受控解密确认后修复；这是防重复下发的生命线
5. **GetReport/GetReply 拉走即消费**：轮询任务必须先把完整响应 AES-GCM 加密落 `raw_vendor_log.payload_enc`，同时只保存不含手机号的 custom_ids 索引元数据；提交事务后再受控解密解析。解析失败保留 processed=false 可重放，raw 表禁止 JSONB 明文手机号
6. 队列可靠性：PostgreSQL 为唯一事实源，Redis 仅投递通道；beat 单实例（启动抢 Redis 锁，抢不到即退出）；reconcile 每 5min 兜底重投，已 submitted/uncertain 的 chunk 绝不重投
7. 幂等：`SETNX idem:{scope_kind}:{scope_id}:{biz_id} = batch_no EX 86400`；DB 使用 `idempotency_record` 的 `(scope_kind, scope_id, biz_id)` 唯一约束与 `expires_at` 兜底。`scope_kind='app'` 时 `scope_id` 必须绑定 `app_id`（`app_id IS NOT NULL AND scope_id = app_id::text`）；account/resend/web-legacy 等其它作用域不受该 CHECK 误伤。事务先删除同键过期记录，再创建批次和幂等记录；唯一冲突回查未过期原批次。`sms_batch.biz_id` 不得永久唯一，确保 24h 后可复用
8. 手机号校验统一 `^1\d{10}$`（11 位）
9. 状态机：
   - batch: pending_approval→(queued|scheduled|rejected|expired)；scheduled→(queued|cancelled)；queued→sending→completed；sending→completed_unknown(uncertain 保守终态)；sending→balance_blocked→queued(人工恢复)
   - chunk: pending→submitting→(submitted|failed|uncertain|split_capacity_blocked|failover_pending)；retrying→submitting；failover_pending→submitting（仅 claim_next_vendor_invoke CAS）；submitting→failover_pending（safe reject 且有合法下一候选）；submitting|failover_pending→failed（无下一候选或策略不允许）；submitting|split_capacity_blocked→failed（仅供应商 1006 原子拆分成功）；split_capacity_blocked 不得回呼厂商，由 reconcile 在有余量后重试同一 split generation；uncertain→submitted(仅 reconcile 证据)或 unknown_terminal(超过 uncertain_max_lifetime_hours)；禁止自动重发，禁止把旧 uncertain 改回 pending，禁止 uncertain 后自动切换供应商；failover_pending 不得按 submitting 超时转 uncertain
   - 非法流转抛 409 STATE_CONFLICT
10. 类别策略集中在 `services/category.py` 单点实现（队列路由/时间窗/黑名单开关/审批阈值/QPS 预留），禁止散落 if-else
11. **配额/频控事实账本（v1.6.5）**：PostgreSQL `usage_reservation` 与明细表是唯一事实源，状态至少覆盖 reserved/committed/release_requested/released/uncertain；同一稳定请求、释放事件和投影版本必须受唯一约束。Redis 只保存带版本的绝对值投影，可从事实重建；marker 缺失、重建中或 Redis 不可确认时发送入口必须 503 失败关闭，禁止把缺失计数当零。驳回/过期/取消/全量剔除/入库失败/幂等复用统一以事务性 Outbox 请求释放，重复消费不得二次回补；号码频控仅 counted=true 的已接受号码计数，HMAC 轮换通过不可逆 alias 归并同一主体
12. 审批回避：接口层校验 approver != applicant（403），DB CHECK 兜底
13. 回调安全：URL 保存与出站前双重校验内网 CIDR 白名单；签名 `X-Sms-Signature = hex(HMAC-SHA256(callback_secret, f"{timestamp}.{raw_body}"))` + `X-Sms-Timestamp`；5s 超时；失败回调（含 4xx，不只 5xx）按 60/300/900/3600/3600s 重试 5 次后置 dead 并告警
14. 写操作全部埋审计（`@audited` 装饰器），审计不得吞异常；**七个运行角色均无 audit_log UPDATE/DELETE/TRUNCATE 权限，且不得是 owner/超级用户；只读 metrics 也无 INSERT**（见 deploy/database-roles.md）。迁移只由独立 sms_owner/migrate 服务执行；应用代码中出现对 audit_log 的 UPDATE/DELETE 即为缺陷
15. 时间一律 TIMESTAMPTZ / ISO8601 +08:00；禁 naive datetime
16. API 错误统一 `{code, message, detail}`；禁止裸 500
17. 库表变更只走 Alembic；手机号相关表新增列必须遵守规则 2 的三列规范
18. **计费条计算只允许 `services/billing.py` 一处实现**（`1 if L≤70 else ceil(L/67)`，L=含签名与退订语的最终内容长度）；配额预扣/回补、预估、统计全部调用它，禁止散落重复实现
19. 号码频控以规则 11 的 PostgreSQL 账本和版本化绝对值投影为准：verify 按号码全局计数，market 按应用与号码计数；窗口对齐分钟/上海自然日，app.freq_override 优先于 sys_config。号码只以 HMAC 参与，保留版本 alias 必须归并为同一主体。新业务复用 `UsageLedgerService`，不得以 Redis INCR+EXPIRE 作为唯一事实；`services/freq.py` 现存直接计数路径的兼容用途与退出需另按调用者及数据事实评估。
20. 营销合规在流水线单点处理：market 内容缺退订语（unsubscribe_suffix）且 unsubscribe_auto_append=true 时自动追加（追加发生在计费条计算**之前**）；Web market 未勾选同意 → 422 CONSENT_REQUIRED；consent_confirmed 与操作人写入审计
21. **审计载荷禁止包含手机号列表**（明文或密文皆禁），只允许号码数量与 batch_no 引用；出现即缺陷
22. 模板渲染 `services/template.py` 单点实现：占位 `{1}..{n}` 全量替换，参数个数不符 422 TEMPLATE_PARAM_MISMATCH，渲染后长度>500 拒绝
23. **模板厂商格式转换（v1.3）**：平台 `{n}` + var_specs(max_len) 提交 BindTemplate 时按序转为厂商 `{s<max_len>}`；渲染时校验每个参数长度 ≤ 对应 max_len（超长即拒，预防厂商 10002）；转换与校验只在 template.py 实现
24. **首个本地管理员初始化（v1.6.1）**：仅空系统允许执行 `init-admin`；默认用户名 `admin`，命令生成 20 位临时密码并在事务提交后仅向当前 TTY 显示一次，首次登录必须修改密码。初始化只创建内置本地账号，与 AD/LDAP 不关联；禁止环境变量名单或其他隐藏提权路径。Codex 可通过 PTY 代执行并把当次密码转告操作者，但密码不得进入命令参数、日志、审计或持久化明文
25. **契约回填（v1.4）**：任何接口的新增/字段变更，同一 commit 内回填 openapi.yaml；PR 自查项——实现与契约 diff 为零
26. **认证方式与浏览器会话**：完整合同见 [PRD 用户与角色](PRD.md#auth-contract)。Web access JWT 走 `Authorization: Bearer`，access、用户快照及高风险短期令牌仅存易失内存；refresh 仅走受限 HttpOnly Cookie。按 Web Locks 能力选择 refresh/access_only，保留 tab binding、单次轮换与重放检测；Access-Only 不得 refresh/replay 或事后升级。会话撤销与权威状态故障失败关闭，AD 完整认证期限不得由 refresh 延长。
27. **verify OTP 打码（v1.4）**：verify 类 content 入库/日志/回调前经 services/masking.py 等长星号替换 4–8 位连续数字（verify_otp_mask=true 时）；**计费与实际下发使用打码前原文**；打码只发生在持久化与外发展示边界
28. **成功率口径（v1.4）**：`delivered/(delivered+failed)`，unknown/other 不入分母；唯一实现 services/stats.py，前端不得自行计算
29. **任务必被追踪（v1.5）**：每个 Celery beat 任务必须以 `@tracked_job("job_name", expect_interval_s=N)` 包装并声明预期间隔；新增任务未包装即缺陷（会成为心跳巡检盲区）；心跳巡检运行在 api 进程内，禁止实现为 beat 任务
30. **异常检测双条件（v1.5）**：突增告警必须同时满足 倍数阈值 与 绝对量下限（services 端断言），防小基数误报；verify 类异常一律 crit 且文案含处置建议
31. **账号 Provider 体系**：显式选择 local/AD，失败不得回退；本地账号与 ldap_real（ldap3）共用规范化身份、失败阈值/IP 限流/JWT 层。`AUTH_MOCK=1` 只替换 AD 校验并走 seed-dev 身份，生产必须为 0。账号、版本化 AD 配置和临时密码合同见 [PRD 用户与角色](PRD.md#auth-contract)；Provider 预留未来 IAM 扩展，本期不实现 IAM。
32. **告警 log-sink（v1.6）**：告警渠道配置为空 ⇒ 只落 alert_log+日志，不外呼；所有告警测试断言 alert_log 行，任何测试不得请求企微/SMTP
33. **令牌桶算法（v1.6）**：单桶容量 vendor_qps、每秒整补；取令牌为 Redis Lua 原子操作，入参 lane∈{realtime,bulk}；bulk 仅当 剩余令牌 > reserved_realtime_qps 时可取，realtime 无此限制；唯一实现 core/ratelimit.py
34. **beat 调度读取时机（v1.6）**：任务间隔在 beat 启动时读 sys_config（缺省用建表默认值），修改间隔需重启 beat 容器生效（界面提示此点）；禁止实现动态热更调度
35. **迁移基准（v1.6）**：Alembic 首个迁移必须以 `schema.sql` 为唯一输入，在同一事务内通过内置解析器按顶层分号无损切分执行，并以逐字重组测试保证原文不变。此后变更必须**手写** Alembic 迁移，并同步回写 `schema.sql` 注释版本；`models/` 不承载声明式 SQLAlchemy metadata，**禁止**把 `alembic revision --autogenerate` 当作权威来源。`scripts_support/check_migration.py` 以 sms_owner 分别构建空库，比对 schema.sql 与完整 Alembic 两种建法的表/列/索引/约束集合，差异即失败；七个运行角色均不得具备 DDL 权限，未来表不得默认授权
36. **前端资源自包含（v1.6）**：字体经 npm 包（@fontsource/ibm-plex-mono 等）或系统栈回退，构建产物不得引用任何运行时外部 CDN
37. **敏感中间产物（v1.6）**：callback_task 只存消息引用与无 PII 元数据，投递时临时构造 body；import 号码逐条落 import_phone 三列；剔除清单只含 phone_mask+原因；decrypted 导出文件仍须 AES-GCM 密文落盘、下载时流式解密
38. **真实联调控制台与受控 API UAT**：正式 Key、测试号码与生命周期操作仅由 admin 的 `/configs`「真实联调」页按 [FR-19a](PRD.md#vendor-uat-contract) 和 [受控手册](docs/runbooks/controlled-real-vendor-test.md) 执行。普通 API 不得获得 root、sudo 或 Docker Socket；live-test 普通发送返回 `VENDOR_TEST_CONSOLE_ONLY`。唯一应用侧真实发送例外为 `POST /api/v1/messages/uat-send`：有效 API Key、notice、单个 active 已登记号码；完整的限流、全版本 HMAC、维护租约、双 critical pause、每日 100 个计费条及 24h 幂等合同不得绕过。
39. **开发与测试部署解耦**：完成功能与必要定向测试后提交并推送短生命周期分支，日常提交前运行 `scripts/dev_check.sh --changed`；测试服务器只在需要共享环境验收时更新，默认对合并后的精确 `origin/main` 执行 `scripts/test_update.sh apply --ref origin/main`，`plan` 仅用于可选预览，`status` 仅用于后续只读诊断。入口只构建受影响镜像，不得重复执行 CI/G2 或组件测试；high-risk、迁移或控制面更新必须在 `apply` 前验证目标 commit 精确且来源为 GitHub Actions 的 `ci-gate=success`。更新入口必须以日常 SSH 更新用户完成远端 Git `origin`、HEAD 和工作树读路径预检；root 能读而 operator 不能读不算通过，必须 fail closed。切换后控制面只对 Git tracked 工作树恢复 operator 的 group 读权限，忽略 secrets/data；operator 还必须以 `git diff --quiet` 读出干净的 tracked 内容和暂存区。只有远端最终返回 `state=verified`、apply 后 operator 再次通过 origin/HEAD/status 读路径预检，同时完成 tracked 工作树与暂存区的 diff 读路径复核，并完成对应表面验收才算成功。无迁移更新不得创建数据库 checkpoint，切换或验收失败时只允许自动回退到上一版应用镜像并记录 `rolled_back`；不得回退 schema。迁移、受保护状态异常或镜像回退失败时保持 fail closed。普通无迁移更新只拒绝 submitting/retrying；high-risk 或迁移更新还必须拒绝 uncertain。分支部署只作为明确的合并前验收例外；若其 tree 与后续 `origin/main` 完全一致且 main 的 `ci-gate` 成功，可用 `promote --ref origin/main` 免重建提升。始终保留 PostgreSQL 数据库、Docker volume 和运行态目录，任何初始化必须事先取得操作者明确确认。管理员初始化、正式厂商 Key 安装/轮换和测试号码管理是独立流程，不得夹带在快速更新中执行
40. **显式运行时与认证边界**：必须设置 `ENVIRONMENT=development|test|production`，且与 DEBUG/Mock 组合不一致时启动失败；生产关闭 Swagger、ReDoc 与 OpenAPI。API client 与 Web user 路由必须分别声明 API Key/Bearer dependency，禁止以路径前缀或 Header 组合猜测认证类型；新增受保护路由遗漏 dependency 必须由契约测试阻断。QPS、导入大小/行数、超时与锁定时间必须有上界及跨字段约束
41. **Redis 故障域与 ACL**：broker、auth、control 必须使用不同 endpoint tuple、独立 ACL 密码、进程/容器、AOF 与数据目录。生产默认 `managed`；单 VM `isolated-standalone` 仅在正式风险批准且业务接受整机故障最长停服 12 小时后启用，不能声称高可用。TLS/CA/主机名校验、容量、持久化、外部告警和最小权限完整合同见 [deploy/redis-ha.md](deploy/redis-ha.md)。API 不得获得 broker secret，worker-callback 不得获得 auth secret；auth 故障 fail closed，broker 失败由 PostgreSQL Outbox 保留事实，control 只从 PostgreSQL 重建。

## 工作方式

- 编码与提交使用本任务独立分支/worktree；本任务已有独立工作树时继续复用，新任务从共享主工作树开始时新建。只读任务无需新建。不得覆盖、暂存、搬走或清理其他会话的修改和未跟踪文件。
- 保持 KISS，优先复用现有实现与入口。非平凡开发或交付先明确目标、风险域和 1–3 条验收；小修改直接完成并简要说明验证。不维护仓库任务清单。
- 在用户已授权的阶段持续完成实现、必要验证和由本次改动引起的修复。常规实现选择自主决定；会改变产品行为、授权范围或不可逆结果的问题再向用户确认。需要确认时先准备完整、可审阅的结果。
- 编码循环使用与改动相关的测试和局部检查；提交前运行 `scripts/dev_check.sh --changed`，并保留 Git hooks。通过后仅在新改动、失败或未解风险出现时扩大或重复验证。该命令按组件选择检查，并不保证只运行改动文件的测试；测试更新不重复 CI/G2，详见规则 39。
- 已确认处于隔离 Mock 环境、不会触及真实服务或删除持久数据的定向测试，可在任务授权内运行、修复和重跑。创建 worktree 不代表 Docker 数据已隔离；reset、删除卷、管理员初始化、凭据操作及真实短信仍按各自授权和入口执行。
- 不确定的产品决策先查 PRD；需要假设时在交付说明或 PR 中明确记录。代码保留类型注解，关键业务函数使用中文 docstring。涉及手机号的新代码遵守规则 2 与 4。
