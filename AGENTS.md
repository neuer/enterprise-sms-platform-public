# AGENTS.md — 企业短信管理平台工程约定 v1.6

本文件是 Codex 与 Claude Code 共用的唯一工程约定。日常开发与交付从
`MAINTENANCE.md` 开始，当前外部阻塞看 `PROGRESS.md`。需求见 `PRD.md`，接口契约见
`openapi.yaml`，数据模型见 `schema.sql`，厂商精确报文与 mock 契约见
`docs/vendor-api.md`。**冲突时以 PRD.md 为准。**

## 技术栈（锁定，勿替换）

- 后端：Python 3.12 / FastAPI / SQLAlchemy 2.x (async) / Alembic / Celery 5 + Redis broker
- 前端：Vue 3 + Vite + TypeScript + Pinia + Element Plus + ECharts
- 存储：PostgreSQL 16（asyncpg）、Redis 7（队列/配额/限流/幂等/JWT黑名单/缓存）
- 部署：Docker Compose；python:3.12-alpine / node:24-alpine（构建）/ nginx:stable-alpine / postgres:16-alpine / redis:7-alpine；生产基础镜像固定 digest，四个最终镜像必须通过独立 Trivy 门禁
- 关键库：ldap3、pyahocorasick、httpx、openpyxl、cryptography（AES-GCM）

## 目录约定

```
backend/
  app/
    main.py
    settings.py        # 可变参数读 sys_config；运行凭据默认 Docker secrets，安全日报 Resend Key 走专用 UI 配置例外
    api/               # messages.py web_messages.py approvals.py templates.py
                       # signs.py replies.py admin.py reports.py auth.py
    services/          # pipeline.py(发送流水线) category.py(类别策略矩阵)
                       # billing.py(计费条,唯一实现) template.py(渲染) freq.py(号码级频控)
                       # quota.py approval.py alert.py blacklist.py sensitive.py
                       # crypto.py(加密/HMAC/掩码) masking.py(OTP等长打码) callback.py stats.py export.py
    vendor/            # zhihui.py mock_server.py codes.py
    models/            # 不承载声明式 metadata；库表以 schema.sql + 手写 Alembic 为准
    tasks/             # send.py poll_report.py poll_reply.py poll_balance.py anomaly.py
                       # scheduler.py reconcile.py(对账) callback.py
                       # housekeeping.py stats.py
    core/              # auth(JWT/APIKey/LDAP) rbac audit ratelimit jobtrack(任务追踪)
  tests/
frontend/
  src/{views,components,api,stores,router,composables}
docs/
  vendor-api.md      # 厂商精确报文与mock契约(适配层唯一依据)  DECISIONS.md(假设记录)
deploy/
  docker-compose.yml(部署契约:服务/队列/secrets名不可改)  nginx.conf
  secrets/(gitignore)  .env.example  seed.example.sql  README.md  dba.md  failover.md
```

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
   - chunk: pending→submitting→(submitted|failed|uncertain|split_capacity_blocked)；retrying→submitting；submitting|split_capacity_blocked→failed（仅供应商 1006 原子拆分成功）；split_capacity_blocked 不得回呼厂商，由 reconcile 在有余量后重试同一 split generation；uncertain→submitted(仅 reconcile 证据)或 unknown_terminal(超过 uncertain_max_lifetime_hours)；禁止自动重发，禁止把旧 uncertain 改回 pending，禁止 uncertain 后自动切换供应商
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
19. 号码级频控集中 `services/freq.py`：verify 全局键 `freq:v:{hmac}:m|d`、market 应用键 `freq:m:{app_id}:{hmac}:d`，Redis 原子 INCR+EXPIRE（对齐分钟/自然日），app.freq_override 优先于 sys_config；键值用 phone_hmac，禁止明文号码进 Redis
20. 营销合规在流水线单点处理：market 内容缺退订语（unsubscribe_suffix）且 unsubscribe_auto_append=true 时自动追加（追加发生在计费条计算**之前**）；Web market 未勾选同意 → 422 CONSENT_REQUIRED；consent_confirmed 与操作人写入审计
21. **审计载荷禁止包含手机号列表**（明文或密文皆禁），只允许号码数量与 batch_no 引用；出现即缺陷
22. 模板渲染 `services/template.py` 单点实现：占位 `{1}..{n}` 全量替换，参数个数不符 422 TEMPLATE_PARAM_MISMATCH，渲染后长度>500 拒绝
23. **模板厂商格式转换（v1.3）**：平台 `{n}` + var_specs(max_len) 提交 BindTemplate 时按序转为厂商 `{s<max_len>}`；渲染时校验每个参数长度 ≤ 对应 max_len（超长即拒，预防厂商 10002）；转换与校验只在 template.py 实现
24. **首个本地管理员初始化（v1.6.1）**：仅空系统允许执行 `init-admin`；默认用户名 `admin`，命令生成 20 位临时密码并在事务提交后仅向当前 TTY 显示一次，首次登录必须修改密码。初始化只创建内置本地账号，与 AD/LDAP 不关联；禁止环境变量名单或其他隐藏提权路径。Codex 可通过 PTY 代执行并把当次密码转告操作者，但密码不得进入命令参数、日志、审计或持久化明文
25. **契约回填（v1.4）**：任何接口的新增/字段变更，同一 commit 内回填 openapi.yaml；PR 自查项——实现与契约 diff 为零
26. **认证方式与浏览器会话（v1.6.4 / D048 修订）**：Web 业务 API 的 access JWT 一律走 `Authorization: Bearer` 请求头，由前端请求层统一注入；access 与用户快照只存当前页面易失内存，禁止写入 Pinia 持久化、sessionStorage、localStorage、IndexedDB、URL 或日志（历史 Web Storage 凭据只可同步迁移一次并立即清除）。refresh JWT 仅以受限路径的 HttpOnly Cookie 承载（生产 Secure、SameSite=Lax）；携带 Cookie 的 refresh/logout 必须做同源 Origin/Referer 校验。注销、401、超时和强制下线必须清理内存会话与 refresh Cookie，并通过不含凭据的跨标签页信号同步。AD refresh family 自最初完整密码认证起默认最多 480 分钟（可配置 15–10080 分钟），refresh 不得滑动延长；到期原子吊销并要求重新登录。本地 family 保持既有 7 天上限。首次改密、step-up 等高风险短期令牌只可存在于组件局部易失内存，禁止写入任何浏览器存储或日志，组件销毁时必须清空
27. **verify OTP 打码（v1.4）**：verify 类 content 入库/日志/回调前经 services/masking.py 等长星号替换 4–8 位连续数字（verify_otp_mask=true 时）；**计费与实际下发使用打码前原文**；打码只发生在持久化与外发展示边界
28. **成功率口径（v1.4）**：`delivered/(delivered+failed)`，unknown/other 不入分母；唯一实现 services/stats.py，前端不得自行计算
29. **任务必被追踪（v1.5）**：每个 Celery beat 任务必须以 `@tracked_job("job_name", expect_interval_s=N)` 包装并声明预期间隔；新增任务未包装即缺陷（会成为心跳巡检盲区）；心跳巡检运行在 api 进程内，禁止实现为 beat 任务
30. **异常检测双条件（v1.5）**：突增告警必须同时满足 倍数阈值 与 绝对量下限（services 端断言），防小基数误报；verify 类异常一律 crit 且文案含处置建议
31. **账号 Provider 体系（v1.6.1）**：Web 登录必须显式选择 local/AD Provider，禁止失败后自动回退；本地账号与 ldap_real（ldap3）共享规范化身份、可恢复失败阈值/IP 限流/JWT 层，用户名失败标记不得在密码校验前阻断，正确凭据只有在权威身份绑定完成后才能清除失败状态；AUTH_MOCK=1 只替换 AD 校验并走 seed-dev 身份，生产必须为 0。登录名使用全局 case-insensitive 唯一空间且先到先得；本地账号仅管理员维护、不开放注册，临时密码首次登录必须修改。AD 非敏感配置仅从系统配置页的版本化草稿读取，bind 密码与 CA 继续走文件边界；Provider 接口预留未来 IAM 扩展，本期不实现 IAM
32. **告警 log-sink（v1.6）**：告警渠道配置为空 ⇒ 只落 alert_log+日志，不外呼；所有告警测试断言 alert_log 行，任何测试不得请求企微/SMTP
33. **令牌桶算法（v1.6）**：单桶容量 vendor_qps、每秒整补；取令牌为 Redis Lua 原子操作，入参 lane∈{realtime,bulk}；bulk 仅当 剩余令牌 > reserved_realtime_qps 时可取，realtime 无此限制；唯一实现 core/ratelimit.py
34. **beat 调度读取时机（v1.6）**：任务间隔在 beat 启动时读 sys_config（缺省用建表默认值），修改间隔需重启 beat 容器生效（界面提示此点）；禁止实现动态热更调度
35. **迁移基准（v1.6）**：Alembic 首个迁移必须以 `schema.sql` 为唯一输入，在同一事务内通过内置解析器按顶层分号无损切分执行，并以逐字重组测试保证原文不变。此后变更必须**手写** Alembic 迁移，并同步回写 `schema.sql` 注释版本；`models/` 不承载声明式 SQLAlchemy metadata，**禁止**把 `alembic revision --autogenerate` 当作权威来源。`scripts_support/check_migration.py` 以 sms_owner 分别构建空库，比对 schema.sql 与完整 Alembic 两种建法的表/列/索引/约束集合，差异即失败；七个运行角色均不得具备 DDL 权限，未来表不得默认授权
36. **前端资源自包含（v1.6）**：字体经 npm 包（@fontsource/ibm-plex-mono 等）或系统栈回退，构建产物不得引用任何运行时外部 CDN
37. **敏感中间产物（v1.6）**：callback_task 只存消息引用与无 PII 元数据，投递时临时构造 body；import 号码逐条落 import_phone 三列；剔除清单只含 phone_mask+原因；decrypted 导出文件仍须 AES-GCM 密文落盘、下载时流式解密
38. **真实联调控制台与受控 API UAT（v1.6.3）**：单机开发测试环境的正式 Key 安装/轮换、加密测试号码管理、激活/暂停/恢复和页面单号码 UAT 的正常入口只能是 admin 的 `/configs`「真实联调」页。高风险操作按当前 Provider 二次认证并签发 5 分钟单用途单次令牌；普通 API 只转发浏览器密文，经固定 Unix Socket 调用 root 管理的 `vendor-control-agent`，不得获得 root、sudo 或 Docker Socket。测试号码以 PostgreSQL 的 `phone_enc/phone_hmac/phone_mask/key_version` 为唯一事实源；live-test 模式下普通发送 API/Web 必须返回 `VENDOR_TEST_CONSOLE_ONLY`。唯一应用侧真实发送例外是 `POST /api/v1/messages/uat-send`：必须使用有效 `X-Api-Key`，仅通知（`notice`）直接内容或已审核模板（`template_id`+`template_params`）、仅一个 active 已登记号码、必填 1–32 位 `biz_id`，禁止定时、验证码、营销和额外字段；必须先消费应用限流并校验通知权限，再以全版本 HMAC 定位号码，所有保留版本 digest 必须完全匹配，最后只把加密四元组交给既有 pipeline。worker 加载分片时与号码维护共享同一 advisory xact lock 并再次验证 active，且 queued/sending 测试批次作为维护租约，终态前禁止停用或删除；API 发现控制状态损坏或过期必须原子写入两个独立 agent-stale critical pause 键，写入未确认即保持 503 且不得继续。页面与 API UAT 共同受每日 100 个计费条、uncertain 占额、critical pause、应用限流和 24h 幂等约束
39. **开发与测试部署解耦**：完成功能与必要定向测试后提交并推送短生命周期分支，日常提交前运行 `scripts/dev_check.sh --changed`；测试服务器只在需要共享环境验收时更新，默认对自动合并后的精确 `origin/main` 执行 `scripts/test_update.sh apply --ref origin/main`，`plan` 仅用于可选预览，`status` 仅用于后续只读诊断。入口只构建受影响镜像，不得重复执行 CI/G2 或组件测试；high-risk、迁移或控制面更新必须在 `apply` 前验证目标 commit 精确且来源为 GitHub Actions 的 `ci-gate=success`。更新入口必须以日常 SSH 更新用户完成远端 Git `origin`、HEAD 和工作树读路径预检；root 能读而 operator 不能读不算通过，必须 fail closed。切换后控制面只对 Git tracked 工作树恢复 operator 的 group 读权限，忽略 secrets/data；operator 还必须以 `git diff --quiet` 读出干净的 tracked 内容和暂存区。只有远端最终返回 `state=verified`、apply 后 operator 再次通过 origin/HEAD/status 读路径预检，同时完成 tracked 工作树与暂存区的 diff 读路径复核，并完成对应表面验收才算成功。无迁移更新不得创建数据库 checkpoint，切换或验收失败时只允许自动回退到上一版应用镜像并记录 `rolled_back`；不得回退 schema。迁移、受保护状态异常或镜像回退失败时保持 fail closed。普通无迁移更新只拒绝 submitting/retrying；high-risk 或迁移更新还必须拒绝 uncertain。分支部署只作为明确的合并前验收例外；若其 tree 与后续 `origin/main` 完全一致且 main 的 `ci-gate` 成功，可用 `promote --ref origin/main` 免重建提升。始终保留 PostgreSQL 数据库、Docker volume 和运行态目录，任何初始化必须事先取得操作者明确确认。管理员初始化、正式厂商 Key 安装/轮换和测试号码管理是独立流程，不得夹带在快速更新中执行
40. **显式运行时与认证边界**：必须设置 `ENVIRONMENT=development|test|production`，且与 DEBUG/Mock 组合不一致时启动失败；生产关闭 Swagger、ReDoc 与 OpenAPI。API client 与 Web user 路由必须分别声明 API Key/Bearer dependency，禁止以路径前缀或 Header 组合猜测认证类型；新增受保护路由遗漏 dependency 必须由契约测试阻断。QPS、导入大小/行数、超时与锁定时间必须有上界及跨字段约束
41. **Redis 故障域与 ACL**：Celery broker、认证/会话/step-up、配额/频控/幂等/业务锁必须使用三个不同 Redis endpoint tuple、三个独立 ACL 密码、三个独立进程/容器、AOF 与数据目录。生产默认使用 `managed` 高可用端点；经正式风险批准、且业务接受整机故障最长停服 12 小时的单 VM 首发方案，允许显式使用 `isolated-standalone`，但不得把它描述为高可用或伪装成 `managed`。该模式仍必须使用 TLS/CA/主机名校验、`noeviction`、独立 maxmemory、固定持久化挂载和外部告警，并承认 VM/内核/磁盘是共同故障域。API 不得获得 broker secret，worker-callback 不得获得 auth secret；default 用户和危险管理命令必须禁用。auth 不可用时 fail closed，broker 失败由 PostgreSQL Outbox 保留事实，control 投影只可从 PostgreSQL 事实重建

## 前端 UI 约定

- 布局：左侧固定导航（附录A 页面清单分组）+ 顶栏（用户/角色/登出）；内容区 Element Plus `el-card`
- 列表页：`el-table` + 顶部筛选区 + 分页（默认20，常量 `DEFAULT_PAGE_SIZE`）；行操作用文字按钮；详情一律右侧 `el-drawer`，不用整页跳转
- 手机号展示统一 `<PhoneMask>` 组件（默认 mask）；授权解密统一 `<PhoneReveal>` 组件（「授权查看」入口，成功提示记审计，明文只存组件易失状态）
- 状态用 `el-tag` 色彩语义：queued/sending=info、completed/delivered=success、failed/rejected=danger、pending_approval/scheduled=warning、balance_blocked/uncertain=danger 深色
- 图表 ECharts；时间显示本地 +08:00 `YYYY-MM-DD HH:mm:ss`；全站中文文案；空态/加载用 Element 内置组件，不引第三方 UI 库
- 前端共享单点：时间格式化 `src/lib/time.ts`；手机号正则/掩码 `src/lib/phone.ts`；类别/角色/状态/厂商审核文案与默认分页 `src/lib/labels.ts`；请求基建 `src/api/client.ts`（`auth.ts` 为 pre-auth 例外）；剪贴板 `src/lib/clipboard.ts`；ECharts 装配 `src/composables/useChart.ts`；壳样式只在 `workspace.css` 聚合入口（由 `App.vue` 全局引入，视图不重复 import），规则本体按主题分片在 `src/styles/workspace/`（@import 顺序即级联顺序，`overrides-light.css` 明亮覆写层必须末位；新增壳样式进对应分片，禁止另起新文件或页面级拷贝），`theme.css` 只留 token、登录页与 Element 覆写。新增同关注点逻辑一律进单点，禁止页面级拷贝

## 平台错误码

| code | HTTP | 场景 |
|---|---|---|
| INVALID_PARAM | 400 | 参数校验失败 |
| UNAUTHORIZED | 401 | Key/JWT 无效或已吊销 |
| AUTH_REAUTH_REQUIRED | 401 | AD 完整重新认证绝对截止已到，Access/Refresh 均须重新登录 |
| STEP_UP_REQUIRED | 401 | 高风险操作缺少有效二次认证 |
| STEP_UP_EXPIRED | 401 | 二次认证令牌已过期或已使用 |
| FORBIDDEN | 403 | 角色/数据权限不足 |
| AUTH_PROVIDER_DISABLED | 403 | 所选认证源未启用 |
| CATEGORY_NOT_ALLOWED | 403 | 应用无该消息类别权限 |
| SELF_APPROVAL_DENIED | 403 | 审批回避：不能审批本人提交 |
| IP_NOT_ALLOWED | 403 | 来源 IP 不在应用白名单 |
| VENDOR_TEST_CONSOLE_ONLY | 403 | live-test 下普通发送入口关闭，仅真实联调控制台/UAT |
| VENDOR_TEST_MODE_REQUIRED | 403 | 该操作仅允许在受控真实联调模式执行 |
| NOT_FOUND | 404 | 资源不存在 |
| STATE_CONFLICT | 409 | 状态机非法流转/重复审批/导入包已使用或过期 |
| IDEMPOTENCY_CONFLICT | 409 | 同一幂等键已用于不同请求 |
| AUTH_CONTEXT_CHANGED | 409 | 日常改密 CAS 失败：安全版本或凭据版本已变化，须重新登录 |
| ACCOUNT_SOURCE_CONFLICT | 409 | 规范化登录名已由其他认证源先占用 |
| LAST_ADMIN_PROTECTED | 409 | 禁止停用或降级最后一个有效管理员 |
| PROVIDER_CONFIG_UNTESTED | 409 | 当前认证源草稿尚未通过连接测试 |
| PROVIDER_CONFIG_STALE | 409 | 测试期间认证源草稿已发生变化 |
| ACCOUNT_LOCKED | 423 | 错误凭据达到账号失败阈值；正确凭据完成权威绑定后可恢复 |
| SENSITIVE_WORD | 422 | 敏感词命中(block) |
| PASSWORD_POLICY_VIOLATION | 422 | 本地密码不符合长度、字符类别或用户名限制 |
| INVALID_PROVIDER_CONFIG | 422 | 认证源非敏感配置格式或范围无效 |
| TEMPLATE_PARAM_MISMATCH | 422 | 模板参数个数不符或渲染超长(v1.2) |
| CONSENT_REQUIRED | 422 | Web 营销未勾选用户同意(v1.2) |
| ALL_FILTERED | 422 | 号码全部被去重/黑名单/频控剔除 |
| QUOTA_EXCEEDED | 429 | 日配额不足 |
| RATE_LIMITED | 429 | 请求频率超限 / 登录IP封禁 |
| PAYLOAD_TOO_LARGE | 413 | 请求体超过 Nginx/ASGI 对齐的字节上限 |
| INTERNAL_ERROR | 500 | 结构化内部错误（禁止裸 500） |
| VENDOR_ERROR | 502 | 厂商适配透传/测试控制台（附厂商数值 code）；不是通用业务 API 的默认 502 |
| AUTH_SESSION_UNAVAILABLE | 503 | 数据库权威会话投影或 Redis 吊销/轮换状态不可用 |
| AUTH_PROVIDER_UNAVAILABLE | 503 | 所选认证源暂时不可用 |
| DEPENDENCY_UNAVAILABLE | 503 | 必要依赖不可用 |
| CONTROL_AGENT_UNAVAILABLE | 503 | vendor-control-agent 或控制状态不可用 |
| CONTROL_AGENT_PAUSE_UNAVAILABLE | 503 | agent-stale critical pause 键写入未确认 |
| IMPORT_UNAVAILABLE | 503 | 导入登记超时或导入面不可用 |
| SECURITY_DAILY_UNAVAILABLE | 503 | 安全日报控制面或数据源不可用 |
| USAGE_PROJECTION_UNAVAILABLE | 503 | 配额/频控事实投影缺失、重建中或 Redis 不可确认 |
| BALANCE_BLOCKED | 503 | 批次/队列状态与熔断结果（余额不足暂停）；不是普通发送 HTTP 响应 |

注：幂等命中不是错误，返回 200 + `idempotent: true`；营销窗外转定时不是错误，返回 200 + `deferred_reason`。

## 常用命令

```bash
docker compose -f deploy/docker-compose.yml up -d
cd backend && uvicorn app.main:app --reload
celery -A app.tasks worker -Q realtime -l info      # 验证码/通知
celery -A app.tasks worker -Q bulk -l info          # 营销
celery -A app.tasks worker -Q callback -l info      # 结果回调
celery -A app.tasks beat -l info                    # 单实例
uvicorn app.vendor.mock_server:app --port 9028
pytest -x -q                                        # VENDOR_MOCK=1 默认
# 禁止 alembic revision --autogenerate（models/ 无声明式 metadata）
# 手写 backend/migrations/versions 新迁移，同步 schema.sql 后：
#   cd backend && uv run python scripts_support/check_migration.py
```

## 附录：厂商接口

**精确报文、字段大小写（小写驼峰，以官方示例为准）、模板 {s长度} 变量格式、拉走即消费语义、mock 契约与完整错误码处理表，全部以 `docs/vendor-api.md` 为唯一依据**，本文件不再复制以免双维护漂移。适配层实现要点回顾：

- 全部 POST + JSON 包络 `{code, msg, data}`；请求体携带 secretName/secretKey
- 退避重试：429/5002/5003（指数 1/2/4/8/16s ≤5次）；延迟重试：1011(30min)、10010(5min)
- 熔断暂停：999 余额不足、1000/1009/5000/10003/10004（crit 告警 + 双队列暂停）
- 1010 IP 校验失败：仅 crit 告警、**不暂停**队列（以 `docs/vendor-api.md` 为准）
- 缩片重试：1006 折半 vendor_batch_size 一次
- **HTTP 超时/网络异常 → chunk uncertain，禁自动重发**（reconcile 按 customId 在 raw_vendor_log 比对修复）
- 其余参数类错误 → chunk failed 不重试，记录 code/msg

## 工作方式

- **编码与提交一律在新建的 git worktree 中进行**：为当前任务创建独立分支与 worktree（如 `git worktree add <path> -b <branch>`），不得在主工作树直接提交；主工作树可能有他人或其他会话的在制改动
- 保持 KISS：不要过度工程化；优先采用现有、最短、可验证的流程解决问题，只有在现有流程无法满足明确需求时才增加复杂度
- 维护期工作由当前用户请求或 Issue/PR 承载，先写清目标、风险域和 1–3 条验收；不维护仓库内任务清单
- 编码循环优先运行相关测试与局部静态检查；`scripts/dev_check.sh --changed` 是提交前检查，不是每次保存后的必跑命令
- 不确定的产品决策：先查 PRD.md，仍无答案则在 PR 描述列出假设，**不要静默自行决定**
- 生成代码带类型注解；关键业务函数写中文 docstring
- 涉及手机号的任何新代码，自查是否违反硬性规则 2 与 4
