# AGENTS.md — 企业短信管理平台工程约定 v1.6

本文件是 Codex 与 Claude Code 共用的工程约定。日常开发与交付按
[MAINTENANCE.md](MAINTENANCE.md) 对应章节执行；涉及外部阻塞时查看
[PROGRESS.md](PROGRESS.md)。按当前任务读取所需材料，同一任务已读取且未变化的内容无需重复加载。

产品行为以 [PRD.md](PRD.md) 为准；接口、库表、厂商精确报文与 Mock 契约分别查
[openapi.yaml](openapi.yaml)、[schema.sql](schema.sql) 与 [docs/vendor-api.md](docs/vendor-api.md)。
平台错误语义见 [PRD 错误码表](PRD.md#platform-error-codes)；安全审查遵循 [SECURITY.md](SECURITY.md)。
文档与实现冲突时记录具体差异，结合有效变更依据判断；不能用过期文档自动降低安全要求。
下列规则的完整合同在链接文档中；精简措辞不代表放宽链接文档的任何要求。

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

标注“静态门禁”的规则由 `scripts/check_invariants.py` 自动检查，新增同类规则优先落成测试或约束（D122）。

1. **运行凭据默认只通过 Docker secrets 文件读取**，清单与挂载边界见 [deploy/secrets.md](deploy/secrets.md)；禁止入环境变量明文、日志或普通 API 响应（callback_secret 仅允许 AES-GCM 密文入库）。settings 读取后去除行尾换行，DB DSN 必须用 SQLAlchemy `URL.create` 组装。仅有两项例外：
   - 安全日报：管理员可在 `/security-daily` 配置 Resend Key 与最多 3 个收件人，Key 明文存专用 `sys_config` 并同步到独立 mailer；不得用于其他凭据，审计只记 configured 状态和数量（[PRD](PRD.md)）。
   - 真实联调凭据：只在页面组件易失内存中短暂存在并立即加密给 vendor-control-agent，不得以任何形式持久化或明文外泄；权威边界见[受控联调手册「页面安装凭据」](docs/runbooks/controlled-real-vendor-test.md#页面安装凭据)。
2. **手机号永不明文持久化**：逐号码记录必须经 `services/crypto.py` 生成 `phone_enc`/`phone_hmac`/`phone_mask`/`key_version`；精确查询走 phone_hmac；文件、JSONB、缓存与日志同样不得留明文。对外解密只允许“详情按角色查看”与“授权导出”；内部受控解密白名单为 raw 解析/重放、callback 投递、发送下发、UAT 定位，以及退订入黑名单（内存解密后经 `protect_phone` 重加密）。严禁把明文再次写入任何持久层（静态门禁：schema 四列）
3. 所有厂商 HTTP 必须经选定 adapter（当前 `vendor/zhihui.py`），业务代码不得直接 httpx 调厂商（静态门禁）；统一超时 10s、连接池、结构化日志。仅当调用前确定性不可用，或协议明确拒绝且 `safe_to_failover=true` 时允许切换供应商；`uncertain`/`submitted` 后禁止自动切换
4. **Send 超时/网络异常 = 结果未知**：chunk 置 `uncertain`，**严禁自动重发或自动换供应商**；由 reconcile 通过 raw_vendor_log.custom_ids 定位并受控解密确认后修复。这是防重复下发的生命线
5. **GetReport/GetReply 拉走即消费**：先把完整响应 AES-GCM 加密落 `raw_vendor_log.payload_enc`，只另存不含手机号的 custom_ids 索引；提交后再受控解密解析，失败保留 processed=false 可重放；raw 表禁止 JSONB 明文（静态门禁）
6. 队列可靠性：PostgreSQL 为唯一事实源，Redis 仅投递通道；beat 单实例（抢不到 Redis 锁即退出）；reconcile 每 5min 兜底重投，已 submitted/uncertain 的 chunk 绝不重投
7. 幂等：`SETNX idem:{scope_kind}:{scope_id}:{biz_id} = batch_no EX 86400`，DB 以 `idempotency_record (scope_kind, scope_id, biz_id)` 唯一约束与 `expires_at` 兜底。`scope_kind='app'` 时 `scope_id = app_id::text` 且 app_id 非空，其它作用域不受该 CHECK 影响。事务先删同键过期记录再建批次与记录，唯一冲突回查未过期原批次；`sms_batch.biz_id` 不得永久唯一
8. 手机号校验统一 `^1\d{10}$`（11 位）
9. 状态机（非法流转抛 409 STATE_CONFLICT）：
 - batch: pending_approval→(queued|scheduled|rejected|expired)；scheduled→(queued|cancelled)；queued→sending→completed；sending→completed_unknown(uncertain 保守终态)；sending→balance_blocked→queued(人工恢复)
 - chunk: pending→submitting→(submitted|failed|uncertain|split_capacity_blocked|failover_pending)；retrying→submitting；failover_pending→submitting（仅 claim_next_vendor_invoke CAS）；submitting→failover_pending（safe reject 且有合法下一候选）；submitting|failover_pending→failed（无下一候选或策略不允许）；submitting|split_capacity_blocked→failed（仅供应商 1006 原子拆分成功）；split_capacity_blocked 不得回呼厂商，由 reconcile 在有余量后重试同一 split generation；uncertain→submitted(仅 reconcile 证据)或 unknown_terminal(超过 uncertain_max_lifetime_hours)
 - 禁止自动重发，禁止把旧 uncertain 改回 pending，禁止 uncertain 后自动切换供应商；failover_pending 不得按 submitting 超时转 uncertain
10. 类别策略（队列路由/时间窗/黑名单开关/审批阈值/QPS 预留）集中在 `services/category.py`，禁止散落 if-else
11. **配额/频控事实账本**：PostgreSQL `usage_reservation` 与明细表是唯一事实源，状态至少覆盖 reserved/committed/release_requested/released/uncertain，稳定请求、释放事件和投影版本受唯一约束。Redis 只存带版本的绝对值投影，可从事实重建；marker 缺失、重建中或 Redis 不可确认时发送入口 503 失败关闭，禁止把缺失计数当零。驳回/过期/取消/全量剔除/入库失败/幂等复用统一经事务性 Outbox 请求释放，重复消费不得二次回补；号码频控仅计 counted=true 的已接受号码，HMAC 轮换经不可逆 alias 归并（[恢复手册](docs/runbooks/usage-ledger-recovery.md)）
12. 审批回避：接口层校验 approver != applicant（403），DB CHECK 兜底
13. 回调安全：URL 保存与出站前双重校验内网 CIDR 白名单；签名 `X-Sms-Signature = hex(HMAC-SHA256(callback_secret, f"{timestamp}.{raw_body}"))` + `X-Sms-Timestamp`；5s 超时；失败（含 4xx）按 60/300/900/3600/3600s 重试 5 次后置 dead 并告警
14. 写操作全部埋审计（`@audited`），审计不得吞异常。七个运行角色均无 audit_log UPDATE/DELETE/TRUNCATE 权限且不得是 owner/超级用户，只读 metrics 也无 INSERT（[数据库角色](deploy/database-roles.md)）；迁移只由 sms_owner/migrate 执行；应用代码对 audit_log 的 UPDATE/DELETE 即缺陷（静态门禁）
15. 时间一律 TIMESTAMPTZ / ISO8601 +08:00；禁 naive datetime（静态门禁）
16. API 错误统一 `{code, message, detail}`；禁止裸 500
17. 库表变更只走 Alembic；手机号相关表新增列必须遵守规则 2 的四列规范
18. **计费条计算只允许 `services/billing.py`**（`1 if L≤70 else ceil(L/67)`，L=含签名与退订语的最终内容长度）；预扣/回补、预估、统计全部调用它（静态门禁，前后端）
19. 号码频控走规则 11 的账本：verify 按号码全局计数，market 按应用与号码计数；窗口对齐分钟/上海自然日，app.freq_override 优先于 sys_config。新业务复用 `UsageLedgerService`，不得以 Redis INCR+EXPIRE 作为唯一事实；`services/freq.py` 现存直接计数路径的退出另行评估
20. 营销合规在流水线单点处理：market 内容缺退订语且 unsubscribe_auto_append=true 时自动追加（在计费条计算**之前**）；Web market 未勾选同意 → 422 CONSENT_REQUIRED；consent_confirmed 与操作人写入审计
21. **审计载荷禁止包含手机号列表**（明文或密文皆禁），只允许号码数量与 batch_no 引用（DB CHECK `ck_audit_payload_no_pii`）
22. 模板渲染 `services/template.py` 单点实现：`{1}..{n}` 全量替换，参数个数不符 422 TEMPLATE_PARAM_MISMATCH，渲染后长度>500 拒绝（静态门禁：占位解析）
23. **模板厂商格式转换**：平台 `{n}` + var_specs(max_len) 提交 BindTemplate 时按序转为厂商 `{s<max_len>}`；渲染时校验每个参数长度 ≤ max_len（预防厂商 10002）；只在 template.py 实现
24. **首个本地管理员初始化**：仅空系统允许执行 `init-admin`；默认用户名 `admin`，生成 20 位临时密码并在事务提交后仅向当前 TTY 显示一次，首次登录必须修改密码。只创建内置本地账号，与 AD/LDAP 无关；禁止环境变量名单或其他隐藏提权路径。Codex 可通过 PTY 代执行并转告操作者，但密码不得进入命令参数、日志、审计或持久化明文
25. **契约回填**：任何接口新增/字段变更，同一 commit 内回填 openapi.yaml；实现与契约 diff 为零
26. **认证与浏览器会话**（完整合同见 [PRD 用户与角色](PRD.md#auth-contract)）：access JWT 走 `Authorization: Bearer`，access、用户快照及高风险短期令牌仅存易失内存；refresh 仅走受限 HttpOnly Cookie。按 Web Locks 能力选择 refresh/access_only，保留 tab binding、单次轮换与重放检测；会话撤销与权威状态故障失败关闭，AD 完整认证期限不得由 refresh 延长
27. **verify OTP 打码**：verify 类 content 入库/日志/回调前经 services/masking.py 等长星号替换 4–8 位连续数字（verify_otp_mask=true 时）；**计费与实际下发使用打码前原文**
28. **成功率口径**：`delivered/(delivered+failed)`，unknown/other 不入分母；唯一实现 services/stats.py，前端不得自行计算（静态门禁）
29. **任务必被追踪**：每个 Celery beat 任务必须以 `@tracked_job("job_name", expect_interval_s=N)` 包装；心跳巡检运行在 api 进程内，禁止实现为 beat 任务（静态门禁：按启动调度表核对）
30. **异常检测双条件**：突增告警必须同时满足倍数阈值与绝对量下限（services 端断言）；verify 类异常一律 crit 且文案含处置建议
31. **账号 Provider**：显式选择 local/AD，失败不得回退；本地账号与 ldap_real（ldap3）共用规范化身份、失败阈值/IP 限流/JWT 层。`AUTH_MOCK=1` 只替换 AD 校验并走 seed-dev 身份，生产必须为 0。Provider 预留未来 IAM 扩展，本期不实现 IAM
32. **告警 log-sink**：渠道配置为空 ⇒ 只落 alert_log+日志；告警测试断言 alert_log 行，任何测试不得请求企微/SMTP
33. **令牌桶**：单桶容量 vendor_qps、每秒整补；Redis Lua 原子取令牌，lane∈{realtime,bulk}；bulk 仅当剩余令牌 > reserved_realtime_qps 时可取；唯一实现 core/ratelimit.py
34. **beat 调度读取时机**：间隔在 beat 启动时读 sys_config，修改需重启 beat（界面提示）；禁止动态热更调度
35. **迁移基准**：首个迁移以 `schema.sql` 为唯一输入；此后**手写** Alembic 迁移并同步回写 `schema.sql`，`models/` 不承载声明式 SQLAlchemy metadata，禁止以 autogenerate 为权威；`scripts_support/check_migration.py` 与迁移基线测试比对两种建法，差异即失败；运行角色不得具备 DDL 权限，未来表不得默认授权
36. **前端资源自包含**：字体经 npm 包或系统栈回退，构建产物不得引用运行时外部 CDN（静态门禁）
37. **敏感中间产物**：callback_task 只存消息引用与无 PII 元数据；import 号码逐条落 import_phone 四列；剔除清单只含 phone_mask+原因；decrypted 导出文件仍须 AES-GCM 密文落盘、下载时流式解密
38. **真实联调与受控 API UAT**：正式 Key、测试号码与生命周期操作仅由 admin 在 `/configs`「真实联调」页按 [FR-19a](PRD.md#vendor-uat-contract) 和 [受控手册](docs/runbooks/controlled-real-vendor-test.md) 执行；普通 API 不得获得 root、sudo 或 Docker Socket；live-test 普通发送返回 `VENDOR_TEST_CONSOLE_ONLY`；唯一应用侧真实发送例外 `POST /api/v1/messages/uat-send` 的完整合同不得绕过
39. **开发与测试部署解耦**：完成功能与定向测试后推送短生命周期分支；测试服务器只在需要共享验收时，按 [MAINTENANCE 按需测试部署](MAINTENANCE.md#按需测试部署) 对合并后的精确 `origin/main` 更新，以 `state=verified` 与表面验收为成功判据；不得回退 schema，任何初始化与凭据操作都不得夹带
40. **显式运行时与认证边界**：必须设置 `ENVIRONMENT=development|test|production`，与 DEBUG/Mock 组合不一致时启动失败；生产关闭 Swagger、ReDoc 与 OpenAPI。API client 与 Web user 路由分别显式声明 API Key/Bearer dependency，禁止按路径或 Header 猜测；遗漏 dependency 由契约测试阻断。QPS、导入大小/行数、超时与锁定时间必须有上界及跨字段约束
41. **Redis 故障域与 ACL**（完整合同见 [deploy/redis-ha.md](deploy/redis-ha.md)）：broker、auth、control 使用不同 endpoint、独立 ACL 密码、进程/容器、AOF 与数据目录；部署模式（managed / 经批准的单 VM 隔离形态）按链接文档选择，单 VM 不能声称高可用。API 不得获得 broker secret，worker-callback 不得获得 auth secret；auth 故障 fail closed，broker 失败由 PostgreSQL Outbox 保留事实，control 只从 PostgreSQL 重建
42. **历史导出与 Broker 职责边界**：approver 的本人历史导出也必须匹配当前非空部门，角色/部门变更不得因历史创建权恢复跨部门或全局下载。worker/beat/dispatcher 使用按职责派生且独立挂载的 broker ACL 凭据，禁止共享 unacked/pidbox 或跨职责入队；消费入口固定白名单及数据库状态复核必须保留，跨队列恢复通过 Outbox；根种子与首次启用要求见 [deploy/redis-ha.md](deploy/redis-ha.md)

## 工作方式

- 编码与提交使用本任务独立分支/worktree；已有独立工作树时继续复用，新任务从共享主工作树开始时新建。只读任务无需新建。不得覆盖、暂存、搬走或清理其他会话的修改和未跟踪文件。
- 保持 KISS，优先复用现有实现与入口。非平凡开发先明确目标、风险域和 1–3 条验收；小修改直接完成并简要说明验证。不维护仓库任务清单。
- 在已授权阶段持续完成实现、必要验证和由本次改动引起的修复；仅对 PRD 无法确定的产品选择、超出授权或不可逆操作确认，确认前先准备完整、可审阅的结果。
- 编码循环使用相关测试和局部检查；提交前运行 `scripts/dev_check.sh --changed` 并保留 Git hooks，通过后仅在新改动、失败或未解风险出现时扩大验证。缺陷按 D122 分级处理。
- 已确认处于隔离 Mock 环境、不触及真实服务或持久数据的定向测试可在授权内运行和重跑。创建 worktree 不代表 Docker 数据已隔离；reset、删除卷、管理员初始化、凭据操作及真实短信按各自授权和入口执行。
- 不确定的产品决策先查 PRD；需要假设时在交付说明或 PR 中记录。代码保留类型注解，关键业务函数使用中文 docstring。涉及手机号的新代码遵守规则 2 与 4。
