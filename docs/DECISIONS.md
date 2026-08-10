# DECISIONS.md — 产品与工程决策记录

> 只记录 PRD 未直接给出、但为消除实现歧义必须固定的决策。PRD 变更仍须回写 PRD；BLOCKED 使用 `BLOCKED-Dnn` 编号并补充三次修复证据。

## D001 分层门禁

- 状态：已由 D057 取代；以下仅保留建设期决策历史。
- 决策：G1 使用 `verify_milestone.sh Mx`，G2 才使用完整 `verify_all.sh`。
- 原因：中间里程碑不可能验证尚未实现的 M4 E2E/性能资产。
- 影响：AUTOPILOT、TASKS、PROGRESS、门禁脚本。

## D002 数据库 owner/app 分离

- 状态：运行态单一账号部分已由 D046 取代；owner 分离原则继续有效。
- 决策：`sms_owner` 只用于 init/migrate/DBA，`sms_app` 用于全部运行态服务且不得是 owner/超级用户。
- 原因：表所有者无法通过普通 REVOKE 实现 audit_log 不可更新/删除。
- 影响：Compose 增加 migrate 服务，生产 secrets 增加 DB owner/app 两件。

## D003 敏感中间产物

- 决策：raw 完整响应加密落库并以 custom_ids 建无 PII 索引；callback 只存引用；import 逐号三列；导出文件密文落盘。
- 原因：手机号永不明文持久化的规则同时适用于 JSONB、文件与任务载荷。
- 影响：schema、服务边界、OpenAPI、UAT。

## D004 可过期数据库幂等

- 决策：DB 唯一性移至带 expires_at 的 idempotency_record，sms_batch.biz_id 不永久唯一。
- 原因：永久唯一索引与“24 小时后可复用”冲突。
- 影响：发送事务、housekeeping、迁移和并发测试。

## D005 性能门禁分阶段

- 决策：API 30 RPS 受理、verify 延迟和最终排空分别测量。
- 原因：vendor_qps=5 时，30 RPS 混合流量不可能同时在 60 秒内全部下发并排空。
- 影响：perf_smoke.py、AUTOPILOT、TASKS。

## D006 数据密钥在单个 secret 内版本化

- 决策：`data_aes_key` 与 `data_hmac_key` 保持既定两个 Docker secret 名；裸 base64 内容视为 v1。轮换时两个文件均改为 `{"active_version":2,"keys":{"1":"...","2":"..."}}`，版本集合和活动版本必须完全一致。
- 原因：满足跨版本解密和 HMAC 精确查询，同时不扩张生产八件套，也不把 key_version 放入环境变量、数据库配置或日志。
- 影响：新写入记录使用 active_version；历史 AES 密文按记录 key_version 解密；轮换期精确号码查询需使用 `hmac_candidates` 覆盖所有仍保留版本。

## D007 verify 实际下发内容以密文作为事实源

- 决策：`sms_batch.content` 只保存 OTP 等长打码后的展示内容；新增 `send_content_enc`，以“2 字节 key_version + AES-GCM 密文”保存实际下发内容。worker/reconcile 只在内存中临时解密，Celery 消息只传 batch/chunk 引用。
- 原因：若只把 OTP 原文放进 Redis/Celery，Redis 丢任务后 PostgreSQL 无法重建下发，违反“PostgreSQL 唯一事实源”；若明文入库又违反 OTP 不落库。密文事实源同时满足可靠重投与规则 27。
- 影响：T1.6 入库、T1.7 worker、reconcile、迁移一致性与密钥轮换测试；任何日志、API、审计和 `content` 展示列均不得返回解密值。

## D008 企业微信告警属于受控外呼基础设施边界

- 决策：`services/alert.py` 与厂商适配器、callback 投递器同列为允许使用 `httpx` 的三个受控外呼边界；其余业务模块仍由静态检查禁止直接使用。告警模块只能调用 `sys_config.alert_wecom_webhook`，固定 5 秒超时，不记录 URL、响应正文或凭据。
- 原因：FR-15 明确要求企业微信渠道，而原静态 allowlist 只列厂商和 callback，与 T3.4 契约冲突；把企微调用伪装进 callback 或改用 urllib 只会绕过边界审查。
- 影响：`scripts/check_invariants.py`、统一告警服务、企业微信渠道测试与安全审计。

## D009 异常检测使用类别配额计数与完整日保守基线

- 决策：现有配额 Lua 同事务维护 `quota:volume:app:{app_id}:{category}:{date}` 计费条计数，所有回补同步递减；异常基线取 `stat_daily.total_segments` 最近 7 个完整自然日均值。少于 7 日视为无基线，使用 `anomaly_min_total×5` 兜底；anomaly 告警使用 24 小时窗口和含日期 dedup_key。
- 原因：PRD 指定应用×类别 Redis 配额计数，但原键只有应用总量；`stat_daily` 又没有小时维度，无法还原历史同一时刻。完整日均值比按时间比例估算更保守，优先满足双条件防误报，同时不扫描业务明细表或新增原始采集链路。
- 影响：`services/quota.py`、所有预扣/回补协议、`services/anomaly*.py`、tracked beat、T4.3 的 app×category `total_segments` 聚合。

## D010 手机号 GET 查询的无 PII 访问日志

- 决策：保留 PRD/OpenAPI 指定的 `GET /web/messages?phone=` 与 timeline 契约；Nginx 使用只含 `$uri`、不含 query string 的专用 access log，Uvicorn 关闭默认 access log。应用只把手机号转换为多版本 HMAC 候选后传给仓储，业务日志不记录原值。
- 原因：接口契约要求手机号作为 GET query，但默认 Nginx/Uvicorn access log 会记录完整请求目标，直接违反“日志不得留手机号明文”。用路径级访问日志保留状态与时延可观测性，同时移除 query 泄露面。
- 影响：`deploy/nginx.conf`、`deploy/docker-compose.yml`、`backend/Dockerfile`、查询服务安全测试与运维日志口径。

## D011 导出采用独立认证分帧 AES-GCM 文件

- 决策：导出文件使用 `SMSX1 + key_version + 重复(length + AES-GCM frame)` 格式，每帧最多 64 KiB；写入阶段只产生密文 `.part`，下载逐帧认证后流式输出。export_task 增加 `started_at` 作为 15 分钟 worker 租约，避免 worker 中断后任务永久卡在 running。
- 原因：单个 AES-GCM blob 必须完整读入并认证后才能安全输出，不满足 10 万行下载的有界内存流式要求；明文临时 CSV 又违反硬规则 37。独立认证分帧同时满足密文落盘、完整性和有界内存。
- 影响：导出文件编解码器、bulk worker、exportdata volume、0005 迁移、下载/清理任务与 DBA 备份口径。

## D012 日报滚动重算三个上海自然日

- 决策：`aggregate_stats` 每 5 分钟在 bulk 队列重算 Asia/Shanghai 的今天及前两个自然日；每个日期用 PostgreSQL advisory lock 串行化，并在单事务内删除旧快照后完整重建。`unknown_cnt` 合并 unknown/other，app 维度值固定为十进制 app id。
- 原因：报告允许最长 48 小时回补，单次午夜聚合会让历史成功率长期陈旧；滚动三个自然日覆盖边界且保持实现简单。异常检测已按十进制 app id 读取 `dim_value`，必须保持一致。unknown/other 均不进入成功率分母，合并到既有列是无 schema 扩张的保守口径。
- 影响：`services/stats*.py`、tracked beat 调度、T4.4 仪表盘、T4.5 报表与 stat_daily 对账验收。

## D013 仪表盘混合部门业务指标与平台运行信号

- 决策：operator/viewer 的消息、审批、uncertain、callback dead 按 JWT dept 限定，approver/admin 查看全量；余额、unmatched 总数、告警标题与任务健康作为平台级摘要对全部角色可见。API lifespan 从 Celery imports 导入任务模块，以装饰器 `JOB_SPECS` 作为任务健康唯一声明源。
- 原因：PRD 明确所有角色可访问仪表盘，同时要求部门数据权限；业务量必须隔离，而平台余额/心跳等运行信号无法按部门可靠拆分。复用 tracked_job 声明避免在 API 再维护一份易漂移的任务间隔表，并让既有心跳巡检真正覆盖 worker 任务。
- 影响：dashboard 服务/API/UI、API lifespan、任务健康巡检与 T4.5b 运维中心。

## D014 报表页导出复用异步明细导出

- 决策：T4.5 的报表查询以 stat_daily 返回聚合数据；“导出明细 CSV”复用 T4.2 的异步 SMSX1 密文导出，只传当前日期/类别过滤器并由 JWT 固化部门 scope。统计查询缺省最近 30 日、最大 366 日，周从周一开始。
- 原因：T4.2 实施计划明确导出入口随 T4.5 页面交付，另建统计文件格式会制造第二套加密、鉴权、清理和审计链路。聚合用于屏幕分析，现有明细导出用于审计/对账，语义清晰且安全边界唯一。
- 影响：reporting 服务/API/UI、导出轮询与下载交互、OpenAPI 和 T4.10 UAT。

## D015 用户管理使用登录时目录快照

- 决策：登录同步时把来源组与最近同步时间保存到 sys_user；用户管理列表只读 PostgreSQL，不在页面请求中实时访问 LDAP。取消 role_override 时用已保存来源组和当前 role_mapping 重新执行 RoleResolver；角色变化先吊销目标用户全部现有 JWT，再事务更新角色与审计。
- 原因：PRD 同时要求 AD 同步状态、来源组、人工覆盖和强制下线，现有 last_login_at 无法表达来源组或可靠恢复映射。目录快照让 AUTH_MOCK 与 ldap_real 共享同一数据流，避免列表可用性依赖 LDAP；先吊销是权限变更失败时仍安全的保守顺序。
- 影响：sys_user 0006 迁移、登录同步、用户管理 service/API/UI、OpenAPI、角色覆盖审计和 UAT 04。

## D016 运维中心按域端点聚合并复用唯一安全链路

- 决策：保留 OpenAPI 的 alerts/raw/uncertain/unmatched/jobs/queue 分域端点，前端 `/ops` 以 Tabs 按需聚合；callback 复用既有端点与组件。sms_chunk 新增 uncertain_since；raw 重放校验 SHA-256 后复用既有 ingest；unmatched 导出通过 export_task 的固定 dataset 扩展复用 SMSX1 密文文件链路。
- 原因：巨型快照端点会耦合分页和故障域；另建 raw 解析、导出文件或任务注册表会形成安全实现分叉。准确的 uncertain 停留时间不能继续用批次创建时间近似，导出和任务声明也必须保持单一事实源。
- 影响：0007 迁移、ops service/API/UI、report/reply ingest、export filter/worker、JOB_SPECS 手动触发与 UAT 16/17/25/27。

## D017 审计采用事务内显式写入与数据库 PII 双保险

- 决策：继续由各业务仓储在同一事务写 audit_log，路由 `@audited` 作为机判覆盖清单；禁止全局中间件复制请求体。新增数据库 CHECK 拒绝审计 JSON 中的 11 位手机号和逐号保护字段；系统参数更新与审计查询在 T4.6 一并闭环，webhook 查询只返回 configured 状态。
- 原因：自动审计请求体会直接扩大手机号、正文和密钥泄露面，也无法保证业务与审计原子提交。单靠代码评审不足以防未来写入 PII，数据库约束能在所有写路径末端 fail closed。configs 是 T4.6 明确要求的“配置写操作审计”，audit/config 两条 OpenAPI 与前端页面又无后续任务承接。
- 影响：0008 迁移、审计/配置 service/API/UI、写端点静态动作清单、OpenAPI、UAT 10/21/22/27 与 T4.10 安全抽查。

## D018 分区 DDL 只在 migrate owner 边界滚动

- 决策：消息/回复月分区不由 Celery worker 创建或删除；独立 migrate 服务在 Alembic 后以 sms_owner 运行受限脚本，创建当前至未来 13 个月并删除超过保留期的分区。每日 housekeeping 以 sms_send 仅清理 raw/unmatched/import/idempotency/job_run，审计完全排除。
- 原因：给运行态 worker DDL 或 owner secret 会直接违反 DB owner/app 分离硬规则；只在首个迁移静态创建月份又会在跨月后中断写入。复用每次部署必经的 migrate owner 边界可兼顾滚动与最小权限，未来 13 个月为低频发布/维护留足缓冲。
- 影响：migrate 启动命令、partition maintenance 脚本、housekeeping tracked job、bulk worker importdata 挂载、deploy/dba.md 与 T4.9 运维文档。

## D020 Prometheus 采用抓取时聚合的共享事实快照

- 决策：`/metrics` 每次抓取从 Redis 读取 realtime/bulk 队列长度，从 PostgreSQL 聚合发送、厂商错误、uncertain、callback、频控与轮询事实，并用请求级 Prometheus registry 输出；不使用 worker 本地全局 counter 或 multiprocess 文件。
- 原因：API 与三个 Celery worker 位于独立容器，本地 counter 无法完整聚合；共享 multiprocess volume 会引入进程退出清理与滚动发布残值。现有 Redis/PostgreSQL 已是跨进程共享事实边界，抓取时只读聚合可在重启后恢复且不增加组件。
- 影响：metrics service/repository/API、OpenAPI、真实 Compose 抓取验收与 T4.9 Prometheus 部署说明。

## D021 部署文档按安全、数据库与网络职责分册

- 决策：`deploy/README.md` 只做生产上线索引；八件 secrets、流式加密备份恢复、厂商主备出口 IP 分别使用独立手册，并提供内网 Prometheus scrape 样例。T4.9a 冷备同步与演练脚本不提前混入本任务。
- 原因：四类操作属于不同审批人与权限边界，单一长文档容易让 DBA 接触厂商密钥或让网络人员接触 owner 凭据。分册也允许机判每个安全红线，避免示例命令把明文 dump 或密码带入磁盘/历史。
- 影响：deploy 文档索引、secrets/backup/vendor 手册、Prometheus 配置样例、T4.9 文档契约测试与后续 T4.9a 引用。

## D022 冷备脚本不自动同步 secrets 或启动备机

- 决策：每日脚本只同步流式加密数据库快照、SHA-256/无 PII manifest、当前 Git 配置包和无密钥 `.env`；八件 secrets 首次与轮换均由密钥系统在主备分别手工落盘并双人复核。远端快照校验后原子发布，但脚本不启动任何备机服务。
- 原因：自动 rsync secrets 会把主机信任与 SSH 账户扩大为全部生产凭据的复制通道；自动启动又可能产生双 beat、双报告轮询和重复下发。冷备恢复与流量切换必须在确认主节点冻结、厂商白名单和变更审批后分步执行。
- 影响：sync/restore Python 脚本、failover 手册、密钥轮换流程、RPO≤24h 自动化与 RTO≤30min HANDOVER 演练。

## D023 PostgreSQL 错误日志保留错误类型但禁止回显语句与 DETAIL

- 决策：PostgreSQL 固定使用 `log_error_verbosity=terse`、`log_min_error_statement=panic` 与 `log_parameter_max_length_on_error=0`；保留错误级别和类型，不记录失败 SQL、绑定参数或可能包含整行数据的 DETAIL。T4.10 运行态安全门禁扫描含 dev profile 的全部 Compose 日志。
- 原因：`audit_log` 数据库 PII 约束能拒绝违规写入，但 PostgreSQL 默认会把失败行和语句写入错误日志，导致“库中无 PII、日志却有 PII”的反向泄露。应用结构化日志与监控已提供业务定位信息，安全红线优先于错误 SQL 原文诊断。
- 影响：deploy Compose/PostgreSQL 运维约定、SEC-07 日志验收、生产日志排障流程；需用审计动作/对象引用定位应用缺陷，不得临时打开错误语句日志。

## D024 sys_config 按请求或任务边界加载一致快照

- 决策：运行策略由 `RuntimePolicy` 单点完成类型、格式和跨字段解析；Admin 更新在全局事务锁和完整 `sys_config FOR UPDATE` 快照内执行最终校验。认证、回调、导入、测试发送与 uncertain 对账在各自请求或任务开始时读取一次。`report_poll_seconds`、`reply_poll_seconds`、`reconcile_interval_min`、`balance_poll_seconds`、`approval_scan_seconds`、`scheduled_scan_seconds`、`anomaly_scan_minutes`、`usage_projection_reconcile_seconds` 八个 beat 调度键由 beat 与 API 在启动时读取，分别固定调度表与心跳预期间隔；修改后必须重启 beat 和 API。Callback 白名单只允许 RFC1918 IPv4 或 `fc00::/7` ULA 的相同/更细子网。
- 原因：每个字段散落转换会造成管理端“保存成功但运行仍用硬编码”或同一任务读到半套配置；并发分字段更新若只做服务层预检会提交非法组合；跨事件循环复用 asyncpg 池会产生 loop 归属错误；宽泛 CIDR 会把 SSRF 白名单退化为公网代理；beat 动态热更则违反 v1.6 的启动时调度契约。
- 影响：Admin 事务最终校验、登录防爆破、callback 超时/重试与 SSRF、导入/审批过期、Web 测试发送、uncertain 告警、任务心跳、系统参数页重启提示与迁移基准。

## D025 报告回调使用不可变事件身份和规范化快照

- 决策：`message.report` 以报告事实字段生成稳定 SHA-256 `event_key`，不可变事件快照进入 `callback_report_event`，`callback_task.event_keys` 只引用事件身份；入队、重放和保留期清理均在数据库锁与约束下协调。
- 原因：只按 message id/time 或可变消息行去重无法区分同一消息的不同报告，也会在并发重放、任务已投递或清理竞态下重复投递或丢失事件；迁移旧引用若存在歧义必须 fail-closed。
- 影响：schema v1.6.11、0012 迁移及旧库成功/歧义测试、raw 重放、callback 聚合、housekeeping 与迁移双建库检查。

## D026 开发测试环境以正式 Key 做受控小流量真实联调

- 决策：开发认证继续使用 Mock，但厂商发送切到固定 HTTPS origin；正式凭据与唯一测试号码只经 TTY 进入 root 私有文件。API 持久化前和 worker 下发前双重校验 HMAC allowlist，PostgreSQL 按上海自然日原子封顶 100 个计费条，uncertain 当日持续占用。activation 只用 GetBalance 预检，任何失败保持暂停，不清库、不删 volume、不自动 restore、不回退 Mock。普通快速更新拒绝厂商、计费、发送和控制面 high-risk 变更。
- 原因：测试环境仍在快速迭代，完整功能正式 Key 会把开发错误变成真实发送、报告误消费或超额成本；双重 recipient guard、数据库额度和 fail-closed 运维控制能把真实影响限制在已登记号码和固定预算内，同时保留快速修复低风险代码的能力。
- 影响：settings、发送 pipeline/worker、live-test 账本、Compose、host wrapper、加密 checkpoint、快速更新分类、CI/G2 专项门禁和运维手册。CI/G2 永远使用 Mock/FakeCarrier；远端 activation/UAT 仍需操作者再次明确授权。

## D027 正式运营商受控联调收敛到系统配置页面

- 决策：单机测试环境的正常联调操作统一进入 `/configs`「真实联调」页。正式厂商 Key 只在 admin 页面组件的浏览器易失内存短暂存在，以 WebCrypto 混合加密封装后由普通 API 原样转发给 root `vendor-control-agent`；API 不持有解密能力、root、sudo 或 Docker Socket。测试号码改以 PostgreSQL 加密四元组为唯一事实源，真实 UAT 只能从控制台按 `recipient_id` 单号码发起；live-test 下普通发送入口返回 `VENDOR_TEST_CONSOLE_ONLY`。高风险操作使用当前 Provider 二次认证与 5 分钟单用途令牌。完整设计证据保存在受限归档；公开快照以本条决策、PRD 和现行代码契约为准。
- 原因：TTY 可以守住 secret 文件边界，但无法满足所有日常操作页面化、可恢复进度和最小权限协作；把 root 能力收敛为严格 Unix Socket 协议，同时让浏览器只发送端到端密文，可在不提升 API 权限的前提下完成安全交付。
- 影响：AGENTS/PRD 安全契约、系统配置页、Provider 二次认证、测试号码 schema、浏览器 WebCrypto、控制 API、systemd agent、worker fail-closed 状态、OpenAPI、Mock-only CI/G2 与远端演练。凭据轮换用 root 私有 previous/new/phase 事务覆盖 active 切换、运行时重建和新旧两次 GetBalance 验证；失败的独立 critical 层不得被既有 manual pause 遮蔽，崩溃恢复成功前禁止 resume。marker 缺失时只有根 `.env` 再次通过严格纯 Mock 验证才可替换首次激活前凭据。测试号码另存不可解密的全版本 HMAC 索引投影，缺版本时 UAT fail-closed，并由管理员在页面重录同一号码刷新。每日 100 个计费条、uncertain 占额和 GetBalance-only 预检继续有效。

## D028 high-risk 开发更新使用完整门禁和受控自举

- 决策：D026 中“普通快速更新直接拒绝 high-risk”由本决策取代。唯一入口仍是 `scripts/test_update.sh --ref origin/<branch>`；high-risk 必须先在隔离 Compose project 完整执行 CI/G2 与真实联调专项门禁，再进入 backend-safe 的暂停、密文 checkpoint、expand-only、不可变镜像和远端 verified 状态机。首次更新 test-update 控制面自身前，先独立安装同 commit、root-owned、manifest 锁定的 host-control bootstrap；driver 只在正常 manager 尚未声明 high-risk capability 时使用该固定 wrapper。禁止 raw Git、rsync、手工 Compose 或风险降级。
- 原因：仅拒绝 high-risk 会让安全控制面无法通过开发阶段唯一更新入口自举；在本地与服务器维护两份分类又会漂移。共享 Python 分类合同、完整门禁提升和一次性可信 bootstrap 可在不接触数据、不跳过生命周期锁的前提下闭环控制面自升级。
- 影响：快速更新 driver/manager/contract、root-owned secure-access 资产、一次性安装器、CI/G2 时长和运维手册。未知/破坏性路径继续 fail closed；管理员初始化、正式 Key、测试号码和真实激活仍是独立流程。

## D029 真实联调增加窄化的应用 API UAT 入口

- 决策：保留普通 `/api/v1/messages/send` 与 Web 发送的 `VENDOR_TEST_CONSOLE_ONLY` 边界，只增加 `POST /api/v1/messages/uat-send`。它必须使用有效 `X-Api-Key`，请求仅允许 `notice`、一个手机号、直接内容或已审核模板（`template_id`+`template_params`）、可选签名和必填 1–32 位 `biz_id`，拒绝定时、验证码、营销和额外字段。API 先消费应用限流并校验通知权限，再要求 live-test 与 agent 新鲜受控，随后用全版本 HMAC 从 PostgreSQL 定位 active recipient，且数据库所有保留版本 digest 必须与当次输入完全一致；明文不进入 SQL、日志或持久层，只把已有加密四元组交给相同 pipeline。worker 加载分片时与号码维护共享同一 advisory xact lock 并再次检查 active recipient，queued/sending 测试批次在终态前阻止号码停用或删除；控制状态损坏或过期通过 Redis 原子命令写入两个独立 agent-stale critical pause 键，写入未确认即保持 503。继续执行每日 100 个计费条、uncertain 占额、应用限流和 24h 幂等。
- 原因：开发测试需要验证真实的 `X-Api-Key → API → pipeline → queue → carrier` 应用接入链路，但直接放开普通发送会扩大类别、批量、定时和模板表面。独立窄入口可以复用生产链路，同时把真实影响固定在已登记号码和单条通知。
- 影响：AGENTS/PRD/OpenAPI、messages API、测试号码 HMAC 查询、审计覆盖、真实联调手册和 high-risk 快速更新门禁。创建入口和发布本身不发送短信；每次真实 API UAT 仍须操作者明确确认。

## D030 安全日报通过 UI 配置并由独立 Resend 伴生容器投递

- 决策：安全日报不复用业务告警 SMTP。管理员在 `/security-daily` 页面维护启用开关、Resend Key 和最多 3 个收件人；Key 写入专用 `sys_config`，收件人写入 `security_daily_recipient`，API 再以原子 JSON 同步到独立 mailer 的配置目录。Key 不进入平台 worker、beat、短信厂商凭据、日报正文、审计载荷或 API 响应；审计只记录是否配置和收件人数。独立非 root mailer 只读取 `resend.json` 与脱敏控制请求，通过固定 `api.resend.com:443/emails` 投递，再把不含正文、地址或凭据的 sent/failed 结果写入 `results`。
- 原因：用户明确授权安全日报采用 Resend Key 的 UI 配置流程，降低部署和日常运维复杂度；保留独立 mailer、固定发信域名、脱敏报告和控制目录，避免把邮件发送实现散落到短信 API/worker。2026-07-26 信息安全负责人已确认：脱敏安全日报正文在 Resend 美国区域保存 30 天符合公司信息安全要求。
- 影响：新增 `security_daily_recipient` 与 Resend Key 配置键、管理员配置 API/页面、API 到 mailer 的配置同步、`deploy/security-report-config` 目录和 0043 迁移。CI、G2 和快速更新仍只使用 Fake/Mock，永不真实外发；`reports.neuer.cn` 的 DKIM/MAIL FROM DNS、子域专用 `p=none` DMARC 监控策略和日报生成/重试语义保持不变。

## D031 导出任务使用稳定主体、固化范围和不可枚举公开 ID

- 决策：`export_task.id` 只供 worker 内部使用，对外状态、二次认证和下载 URL 统一使用 `public_id` UUID。授权始终要求 `creator_account_id` 与 `scope_resolved` 已确认：admin 可访问全部已解析任务；approver 可访问本人或同部门任务但不得跨部门；operator/viewer 只可访问本人掩码任务。明文任务只允许当前 approver/admin，下载前必须重新认证当前 Provider，并消费绑定 account/identity/JWT jti/IP/public_id 的五分钟单次令牌。授权、有效期和 PII-free 下载审计在同一 PostgreSQL 事务完成。
- 原因：用户名与可枚举整数不能作为对象授权边界，原有 `creator=actor OR elevated` 会把 approver 误扩为跨部门全局访问；只在创建阶段固化部门也无法保护后续状态查询和文件下载。历史任务只有在账号名唯一映射且 filters 明确包含 `scope_dept`（包括显式全局 null）时才可安全恢复授权。
- 影响：schema v1.6.23、0024 expand/backfill/contract 迁移、导出 service/repository/API、双前端与 OpenAPI。无法解析的历史任务对所有角色 fail closed；旧整数 URL 直接退役，不提供宽松兼容入口。downgrade 明确阻止恢复旧授权，后续仅可在导出保留期届满且确认无引用后另行移除兼容展示字段 `creator`。

## D032 JWT 权限上下文以数据库投影和统一安全版本为准

- 决策：access JWT 固定 15 分钟；refresh JWT 最长 7 天，以 Redis family 状态和 Lua CAS 实现单次原子轮换，旧 refresh 重放即吊销整个 family。每次 access 验证及 refresh 都按稳定账号/身份读取数据库，逐项校验账号、身份、Provider 启用状态、登录名、部门、角色和 `security_version`。账号/身份/Provider/外部角色映射触发器与密码重置、强制下线写路径在同一事务递增版本；数据库或 Redis 不可用时返回 503 并 fail closed。
- 原因：仅校验 JWT 自带角色和账号 `auth_version`，无法覆盖 LDAP 部门、身份启停、Provider 禁用及角色映射变化；长生命周期 access JWT 也会扩大权限撤销窗口。服务器端单次 refresh 状态同时限制重放，并让双前端在短 access 到期后更新权威用户摘要。
- 影响：schema v1.6.24、0025 不可降级迁移、认证 service/API、OpenAPI、PRD 与经典版/青鸾版会话存储及 401 单飞刷新。refresh 只存当前标签页的 `sessionStorage`，不用 Cookie；明确的二次认证 401 不触发刷新，`AUTH_SESSION_UNAVAILABLE` 保留浏览器会话以便稍后重试。

## D033 授权、所有权、职责分离与审计只认稳定主体 ID

- 决策：人类请求在 API 边界转换为结构化 `SecurityPrincipal(account_id, identity_id, login_name, dept, role)`，API Key 请求转换为稳定应用主体。`approval`、`export_task`、`import_task`、`vendor_test_operation`、`sms_batch` 与 `audit_log` 双写稳定 ID 和事件时展示快照；本人审批、资源所有权和 operation 合同只比较 `account_id`，审计按 `actor_account_id` 查询。迁移只回填载荷中已有的可信稳定 ID 证据；用户名、显示名、部门或角色均不得用于历史主体反查。
- 原因：外部目录允许同一账号改名，登录名也可能被新账号复用。字符串判断会导致本人审批绕过、历史资源失联或被错误继承，并使审计链无法稳定串联。账号 ID 表示授权主体，身份 ID 保留当次 Provider 身份证据，两者与展示快照必须分离。
- 影响：schema v1.6.25、0026 expand/dual-write/read-new 迁移、审批/导出/导入/发送/真实联调/审计服务、OpenAPI 与双前端。D031 中按当前唯一登录名回填导出创建人的旧策略由本决策取代：升级前缺少 `identity_id` 证据的主体统一重置为 unknown 并 fail closed。downgrade 不得恢复任何用户名安全判断。

## D034 日常 CI 将完整 G2 从普通 PR 阻塞链路移出

- 决策：保留本地和远端同一 `scripts/verify_all.sh` 权威能力。普通后端业务 PR 只执行快速 backend/frontend 检查；迁移/部署、认证/授权/审计、加密与 PII、发送/厂商链路、CI/门禁及未知路径仍在 PR 强制完整 G2。可靠且路径已知的 `main` push 不重复 G2；人工 `workflow_dispatch`、每日定时和发布候选继续执行完整门禁。`test_update` 只验证目标 commit 的 `ci-gate`，不再重复同一门禁。
- 原因：近期 Hosted G2 单次约 22–23 分钟，且串行等待快速 job 后使普通高风险分类 PR 的反馈接近 27 分钟；G2 的运行态安全、E2E、性能与恢复烟测仍有独立价值，但把所有后端生产文件一概纳入会让大量普通业务改动重复静态、单元和前端检查。按真实风险分级可以缩短日常反馈，同时保留跨组件和交付边界的兜底。
- 影响：GitHub CI 路径分类、工作流契约测试与 BOOTSTRAP 说明。`verify_all.sh` 的 11 个阶段、阈值、顺序和失败关闭语义不变；AUTOPILOT 仅保留为历史建设终局协议，日常流程见 D040。公开主仓库继续以 `ci-gate` 作为 `main` 的唯一 required check，禁止绕过门禁直接推送。

## D035 PostgreSQL Transactional Outbox 成为跨边界发布事实

- 决策：批次 ready、审批后续、定时到点、配额补偿、callback ready 与外部告警投递先在原业务事务写入无 PII `outbox_event`。独立 `outbox-dispatcher` 通过 `FOR UPDATE SKIP LOCKED` 领取唯一租约，以 event UUID 作为 Celery task ID；消费者再以 event ID 领取执行租约并用 fencing CAS 完成。首次 broker 发布失败不改变已提交业务的确定受理结果；原 reconcile/due 扫描继续存在，但只作为审计和旧数据兜底。
- 原因：数据库提交后直接调用 broker 会产生不可原子提交裂缝，让 API 返回失败但后台最终仍可能发送，且无幂等键的重试可能创建第二批。把发布意图放入同一 PostgreSQL 事务后，publish 前后崩溃都可恢复；重复 broker 投递由同一 event ID 和业务状态机收敛。
- 影响：schema v1.6.26、0027 expand 迁移、独立 dispatcher 服务、发送/审批/定时/callback/告警/配额补偿任务、管理员 dead-letter 重推与运维指标。旧 worker 单参数任务合同继续兼容；降级前必须确认无未完成事件，禁止丢弃积压。

## D040 callback/export worker 使用数据库 UUID fencing

- 决策：`callback_task` 与 `export_task` 每次领取生成新的 `lease_id`，以 `lease_expires_at` heartbeat 续租；完成、失败、重试和 dead 迁移都必须同时匹配当前 UUID 且租约未过期。过期接管替换 token 并累计 `takeover_count`，CAS 未命中只追加无 PII `worker_lease_event`，不得回写业务状态。
- 回调：`callback_task.event_id` 跨所有重试与接管保持不变，同时进入 canonical body 与 `X-Sms-Event-Id`，由接收方据此幂等；`callback_task.correlation_id` 从触发请求/任务固化并通过 `X-Sms-Correlation-Id` 外传，用于串联 HTTP→Outbox→worker→callback。旧 worker 失去 heartbeat 后取消在途 HTTP 协程，且无法加载新租约的回调素材。
- 导出：`.part` 与 `.smsx` 文件名都包含 `lease_id`；只有当前租约可把完整密文文件路径写入 `export_task`。旧 worker 的清理仅指向自己的租约文件，不能覆盖或删除新 worker 文件。
- 观测：`sms_worker_stalled_leases` 显示待接管租约，`sms_worker_lease_events` 显示 acquired/takeover/heartbeat_lost/fencing_miss/dead/manual_retry 事实；管理员回调列表显示停滞与接管次数。处置流程见 `docs/runbooks/worker-fencing-recovery.md`。
- 影响：schema v1.6.28、0029 expand 迁移、callback/export worker、密文导出文件名、回调管理契约与真实 PostgreSQL 并发门禁。

## D041 厂商报告以不可变事实驱动单调投影

- 决策：GetReport/GetReply 解析结果先分别写入只追加的 `report_event` / `reply_event`，由 64 位事件键主键在 PostgreSQL 内完成并发去重。报告投影按 `(状态强度, reportTime, 同时刻状态优先级, event_key)` 严格单调：成功/失败/余额不足为强终态，其他次之，未知最弱；同强度先比较厂商事件时间，同一时间固定为成功 > 余额不足 > 失败 > 其他 > 未知，最后由事件键稳定打破完全同级冲突。更旧的同强度状态、任何更弱状态均不能覆盖强终态。
- 派生边界：每个报告事实只允许一条 `report_event_projection`；只有消息投影实际变化才刷新批次统计、评估失败率并创建 message.report callback。被忽略的乱序事实仍保留并可经 `raw_id` 审计重放。回复事件键使用 keyring 中最老保留版本的号码 HMAC，并把时间规范到 UTC，因此活动版本轮换和等价时区表示不改变业务事件键。
- 历史数据：升级只把现有投影回填为 `raw_id=NULL` 的兼容事实，不自动猜测或删除旧重复回复。只读命令 `vendor-event-duplicate-audit` 会受控解密后跨 HMAC 版本归组，但输出仅含事件键、mask 与计数；修复仅删除重复 `sms_reply` 业务投影，永久保留 `reply_event` 事实和审计记录。完整流程见 `docs/runbooks/vendor-event-dedup-repair.md`。
- 影响：schema v1.6.29、0030 expand/backfill 迁移、report/reply ingest 与仓储、callback/统计派生条件、真实 PostgreSQL 乱序及并发门禁。

## D042 Web 导入包使用可恢复预留，并在批次事务内完成消费

- 决策：`import_task` 的业务状态改为 `ready → reserved → consumed`。预留由数据库行锁串行化，持久化随机 `reservation_id`、稳定 `reserved_by_account_id`、开始时间和 5 分钟到期时间；活动预留冲突，过期预留可由下一请求原子抢占。校验或批次创建失败按 reservation/account 条件释放为 ready，进程失联则由租约到期恢复。
- 事务边界：批次、消息、审批/Outbox、审计全部写入后，在同一 PostgreSQL 事务内把仍有效的预留更新为 consumed 并固化唯一 `consumed_batch_id`；预留过期或被抢占会使整个批次事务回滚。客户端在提交后丢失响应时，再次提交同一 import_id 直接读取该批次的既有响应，不解密号码、不重复进入发送流水线。
- 兼容与保留：旧 `used=true` 记录在迁移中立即缩短为已过期，失败关闭且不猜测历史批次；`used` 仅保留为滚动升级兼容列，新代码不再读写。手机号密文只在预留读取后于接口局部内存短暂解密，数据库、日志和响应不新增明文边界。导入包到期后，housekeeping 删除 `import_phone` 和临时剔除文件并写 `payload_purged_at`，但永久保留不含 PII 的 consumed→batch 绑定，使超时恢复不会随 24 小时载荷保留期失效。
- 影响：schema v1.6.30、0031 expand/backfill 迁移、Web 导入仓储、发送流水线事务与真实 PostgreSQL 并发/崩溃恢复门禁。

## D036 PostgreSQL 成为配额与号码频控的唯一事实源

- 决策：受理先在 PostgreSQL 创建稳定 `usage_reservation`，按不可逆号码主体、上海自然日和分钟窗口串行写入配额/频控事实；批次保存与 committed 状态同事务完成。Redis 仅接收带单调版本的绝对值投影，旧版本不能覆盖新版本；缺失 ready marker、有事实的投影重建中或 Redis 不可确认时，所有发送入口返回 `USAGE_PROJECTION_UNAVAILABLE`，不把缺失计数当零。
- 原因：仅依赖 Redis TTL 计数无法证明 DB 保存失败、终态补偿、worker 崩溃和 Redis flush 后是否已经扣除或回补，容易造成重复放量或永久占额。稳定预留、唯一释放事件与 Outbox 消费可把每种裂缝收敛为可恢复状态，并允许从 PostgreSQL 重建缓存。
- 影响：schema v1.6.27、0028 迁移、pipeline、审批过期/驳回、定时取消、Outbox、housekeeping、CLI、漂移指标和告警。HMAC 轮换用 alias 归并同一主体；解释、审计、日志和告警只输出聚合或主体 UUID，不输出手机号和 HMAC。安全重建流程见 `docs/runbooks/usage-ledger-recovery.md`。

## D037 快速更新持续验证受控基线到唯一迁移头

- 决策：CI 必须从受控测试环境已记录的兼容基线 `0021_approval_legacy_default` 持续静态验证到仓库唯一迁移头，而不是只检查最新单个迁移。快速更新只接受可静态证明的字面量 SQL；数据回填允许 `UPDATE/INSERT`，触发器或约束替换必须在同一迁移成对出现，动态 SQL、删除数据、收缩列和重命名仍 fail closed。`0025` 在首次进入受控环境前改为保留 `auth_version` 与 `security_version` 双列，并以有序触发器事务级同步；旧、新 writer 在滚动窗口内均可安全工作。
- 原因：受控环境停在 0021 时，原门禁直到远端 prepare 才逐个遇到 0023 的动态 SQL和 0025 的列重命名，导致完整 G2 已绿但发布仍在 `migration_check` 阶段失败关闭。只验证某个历史终点也会在新增迁移后静默漏检。双列同步把 contract 延后到全部旧 writer 已退场且有独立迁移/发布证据之后。
- 影响：快速更新迁移检查器、0025/0026、schema v1.6.27-hotfix、迁移双建库合同和真实 PostgreSQL 安全会话测试。受控更新仍保留数据库、volume 和运行态目录；检查失败不得 stamp、手工改库或绕过唯一入口。将来增加迁移时，唯一头测试会自动把新 revision 纳入 0021→head 的完整兼容列车。

## D038 host-control 资产集合同时约束路径分类与精确提交绑定

- 决策：快速更新合同以单一 `HOST_CONTROL_PATHS` 集合把全部 root-owned host-control 源资产归为 high-risk；shell driver 的固定资产数组必须由测试逐项与该集合一致。public cutover 可继续把已验真的发布元数据剥离为非运行态，但任何未在例外集中的 host-control 资产必须先通过 high-risk 分类，随后由 capability 的 `source_commit` 与目标 commit 字节级比较；发生变化时只接受已安装的同目标 commit 快照。
- 原因：迁移检查器已按精确目标提交安装，但 `check_test_update_migration.py` 和 `run_with_lifecycle_lock.py` 未纳入路径分类，导致 cutover 在到达已有的精确 host-control 校验前被通用 `deploy/` 禁区拒绝。分别维护 Bash 资产表和 Python 风险表会在新增控制面文件时再次漂移。
- 影响：普通 `deploy/` 未知路径仍 fail closed，不新增上传源码、手工 Compose 或风险降级入口。host-control-only 变更也会执行 high-risk 门禁；切换运行 checkout 前仍必须先从服务器取得并安装已审核目标 Git object，安全入口保持 inactive。

## D039 API 固定双进程并以数据库会话租约选举心跳巡检实例

- 决策：生产 API 镜像与 Compose 合同固定启动两个 Uvicorn worker，为强制性能验收保留稳定并发余量；性能阈值、负载与采样口径保持不变。每个 API worker 仍在 lifespan 内启动心跳巡检服务，但只有持有 PostgreSQL 会话级 advisory lock 的进程执行巡检，非领导进程定期尝试接管；进程或连接退出时数据库自动释放锁。
- 原因：同一提交在低算力 Hosted runner 上连续两次仅因验收 P95 超过 300ms 失败，而隔离本地完整负载 P95 为 22ms，说明单 API 进程缺少跨执行环境的容量余量。直接增加进程若不选主，会重复扫描任务状态和产生无意义告警竞争；会话锁可在不新增 beat 任务的前提下提供崩溃释放的单实例语义。
- 影响：API 容器基线增加一个应用进程及一条领导者专用数据库连接；健康检查、端口和安全边界不变。任务心跳巡检仍运行在 API 进程内，告警存储的原子去重继续作为末端保险。

## D040 日常维护复用 CI 证据并把恢复成本限制在实际风险

- 决策：日常入口改为 `MAINTENANCE.md`。测试发布只接受同仓库、已推送且 `ci-gate` 成功的
  不可变 commit，只构建受影响镜像，不重复组件测试或 G2。CI 与测试发布共用受保护路径
  分类。无迁移更新不创建数据库 checkpoint；普通后端无迁移只等待 submitting/retrying，
  high-risk 或迁移才额外等待 uncertain。有迁移仍要求 expand-only 和密文 checkpoint。
  无迁移切换/验收失败自动恢复旧 commit 与应用镜像并记录 `rolled_back`；schema 永不自动
  回退。`main` 以 `ci-gate` 为唯一 required check。
- 原因：原流程在 PR、test_update 和生产 promotion 多次执行相同测试/扫描，并让所有后端
  改动承担整库 checkpoint、uncertain 清零和人工修复成本，反馈时间与真实风险不匹配。
  commit 级 CI 证据、不可变 image ID 和运行态健康检查已经能证明未改变的事实，无需重复
  计算；数据库迁移、PII/密钥和真实发送边界仍需保留严格保护。
- 影响：测试更新 driver/合同/状态机、CI 分类、AGENTS/CLAUDE/BOOTSTRAP/AUTOPILOT 与运行
  手册。管理员初始化、正式 Key、测试号码、数据库和 volume 仍完全独立且默认保留。

## D041 发布镜像只扫描一次并自动生成生产清单

- 决策：候选四镜像内容执行一次 digest 固定 Trivy HIGH/CRITICAL 扫描；promotion 回拉最终
  RepoDigest 后必须逐项证明 image ID 与候选一致，随后复用候选扫描摘要，不对同一内容二次
  扫描。production `manifest.json` 由 `scripts/create_release_manifest.py` 从最终证据自动
  生成并经原 exact-field 合同自校验。数据镜像门禁只在 PostgreSQL/Redis changed 时运行，
  PostgreSQL changed 才要求备份变更与恢复报告。
- 原因：RepoDigest 与 image ID 的内容地址绑定使二次扫描不增加独立信息，手工抄写四镜像
  ref/ID 反而引入漂移和操作错误。把门禁绑定到同一内容、把清单机械字段自动化可缩短生产
  准备，同时不降低漏洞、备份、迁移和运行态验证。
- 影响：`verify_release.sh`、发布证据 writer、生成器、发布文档与合同测试。registry 身份
  验证失败、image ID 不同、候选报告被修改或缺失时仍 fail closed。

## D042 数据库连接按进程和组件复用有界池

- 决策：API、Celery worker 子进程、beat 启动读取、metrics 与独立后台进程使用显式连接
  预算、获取超时、连接超时和 statement timeout。仓储不得自行创建 engine；历史
  `dispose()` 只作兼容 no-op，唯一关闭入口在 FastAPI lifespan、worker child shutdown、
  beat 启动读取结束和后台进程退出。Celery 每个 prefork child 使用一个持久 asyncio loop，
  fork 后丢弃继承 pool，滚动退出时先关闭 DB/Redis 再停止 loop。
- 原因：按请求或按任务创建 `NullPool` engine 无法限制连接风暴，也无法稳定观测等待、超时
  和退出泄漏；把异步 pool 跨 `asyncio.run()` 生成的事件循环复用又会产生 loop affinity
  故障。进程级持久 loop 与固定预算同时解决复用和隔离。
- 影响：连接总预算按 OS 进程数计算；默认 Compose 上限为 52，并须为迁移和运维会话预留
  PostgreSQL 容量。`/metrics` 按低基数组件暴露连接、预算、获取、等待、超时和 shutdown
  泄漏；容量核算、故障恢复与 24 小时混合负载留证见数据库连接池手册。

## D043 存活与就绪分离，运行容器默认最小权限

- 决策：`/livez` 只检查 API 进程事件循环；`/readyz` 在总超时、排队超时和并发闸门内，
  验证运行 secrets、数据库迁移、关键配置、当前及未来至少三个月分区、Redis 与启动调度
  配置。所有失败只返回固定 `not_ready`，日志只记录异常类型。长期运行容器统一显式非 root
  UID、只读根文件系统、`cap_drop: ALL`、`no-new-privileges`、资源上限和最小 tmpfs。
- 原因：旧 `/healthz` 在数据库不可用时仍返回 200，又因启动期数据库异常直接终止进程，
  无法区分“进程存活但不可接流”和“进程已死”；默认可写根目录及未声明资源边界扩大了故障
  和入侵半径。依赖故障时无界探针还可能反向制造连接风暴。
- 构建证据：四个最终镜像继续固定基础镜像 digest，删除运行时无版本系统升级，构建统一
  `linux/amd64`、无缓存、关闭易变 provenance/SBOM 附件并设置 `SOURCE_DATE_EPOCH=0`；
  CycloneDX 去除时间/UUID 后规范排序。正式 release gate 对同一 commit 做独立重建并比较
  四镜像 ID 与四份规范 SBOM 摘要，任一漂移失败关闭。
- 影响：旧 `/healthz` 仅作为 deprecated 存活别名保留；Compose、远端接流、G2、本地启动
  与恢复手册改用 `/readyz`。Web 改用非特权 8080，并显式代理三个健康路径。新增服务必须
  登记相同运行边界，不能通过恢复 root、可写根或 capability 解决启动问题。

## D044 生命周期维护脱离发布，备份只有经随机恢复验证后才可用

- 决策：分区维护除 migrate 后执行外，增加每日 root systemd timer，经受控
  `sms-compose partition-maintenance` 入口使用 sms_owner、事务 advisory lock、固定 public
  schema/分区名/RANGE bound、dry-run、三次退避和安全审计维护窗口。每日生成本地流式加密
  快照并验证逐文件 SHA-256；每周从完整性已验证快照随机选择一份恢复到临时
  `sms_drill_*` 数据库，验证迁移、关键结构、运行角色权限和只读业务查询后立即销毁。
- 可用性：新快照默认不可用。只有恢复演练及 RTO 通过才在 0600 原子生命周期账本标记
  `available=true`；任一演练失败立即撤销。账本持续记录最后备份/演练、备份与演练年龄、
  恢复耗时、数据缺口和保留边界；每小时失效检查和所有 service 失败仅向 journal 输出固定
  操作与异常类型，不记录路径、手机号、密钥或命令错误。
- 原因：发布触发的分区预建会在长期无发布时失效，文件哈希也不能证明数据库可恢复。
  独立 timer 消除对发布频率的依赖，随机恢复与 fail-closed 可用性把 RPO/RTO 从手册声明
  变成持续机器证据。
- 影响：受控 Compose 包装器、分区维护脚本、加密同步/恢复脚本、systemd assets、DBA、
  备份恢复与故障切换手册。`current` 只代表最新密文，不再被解释为可恢复快照。

## D045 环境模式与路由认证必须显式声明

- 决策：运行环境由必填 `ENVIRONMENT` 决定，不再从 DEBUG 反推。生产关闭
  Swagger、ReDoc 和 OpenAPI；API Key 认证从按 URL/Header 猜测的全局中间件迁到
  路由 dependency，混合批次路由显式声明可选 API Key 与 Bearer 两种入口。
- 原因：路径前缀和 Header 组合会使新增/改名路由存在遗漏或绕过窗口；隐式环境和
  无上界配置会把拼写错误或异常值变成不安全默认。
- 影响：部署 `.env`、测试更新与发布门禁分别声明 development/test/production；
  认证路由清单、生产文档关闭和运行参数边界均由自动化契约测试阻断回归。

## D046 数据库运行身份按七个职责隔离

- 决策：D002 的单一 `sms_app` 运行身份由 `sms_auth`、`sms_accept`、`sms_send`、
  `sms_callback`、`sms_export`、`sms_scheduler`、`sms_metrics` 取代；每个角色使用
  独立 Docker secret 和显式 table/sequence/operation 授权。旧 `sms_app` 永久
  NOLOGIN 且无权限，downgrade 不恢复它。
- 原因：单一运行账号会让 callback、metrics 或任一 worker 的凭据泄露横向扩大到账号、
  Provider、系统配置和全部业务写入；默认广泛 DML 还会使未来新表静默继承权限。
- 影响：Compose 增加迁移前角色 provision，仓储按职责选择结构化 DSN，schema 与
  Alembic 同步维护显式矩阵；迁移、G1/G2 和隔离恢复均验证越权拒绝、audit 不可篡改及
  未来表默认无授权。

## D047 首次改密令牌以 PostgreSQL 事务状态为准

- 决策：短期 JWT 只作为不透明载体；服务端仅持久化其 SHA-256 指纹，并绑定
  `account_id`、`identity_id`、local Provider、用途、规范化登录名、签发时
  `security_version` 和到期时间。首次改密先在事务外完成 Argon2id 计算，再在同一
  PostgreSQL 事务中锁定账号与令牌，消费令牌、更新密码、清除首次改密标志、递增安全
  版本、撤销同账号其他改密令牌并写无敏感审计。
- 原因：Redis 先消费再更新数据库会在数据库失败时永久耗尽令牌；把消费事实与密码写入
  同一事实源，能让失败完整回滚，并由账号行锁与安全版本保证并发请求只有一个成功。
- 影响：schema v1.6.34、0035 仅在令牌表为空时可安全降级、认证 JWT/Facade/用户仓储、
  OpenAPI 与真实 PostgreSQL 故障/并发门禁。Redis 故障不再影响首次改密，既有
  access/refresh 仍由数据库权威安全版本立即失效。

## D048 Bearer 会话与高风险令牌使用分级浏览器边界

- 决策：继续使用 `Authorization: Bearer`，不引入 Cookie/CSRF 混合模式。普通
  access/refresh JWT 仅进入当前标签页 `sessionStorage`；注销、401、超时和强制下线清除
  当前标签页，并只用不含凭据的 `storage` 信号通知其他标签页。首次改密、明文导出 step-up
  等高风险短期令牌只存在于组件局部变量，提交、到期或组件销毁立即清空。
- 传输边界：生产 HTTP 入口只能跳转到配置的同一 HTTPS origin；无凭据部署探针验证
  TLS 1.2+、证书剩余期限、HSTS 和 CSP。CSP 禁止内联脚本、脚本属性与内联 style 块；
  Element Plus/ECharts 动态布局暂时仅保留 style attribute 例外。nonce/hash 对同源静态脚本
  和动态 style 属性不能提供额外有效覆盖；Trusted Types 在无 HTML 字符串注入点的现状下
  暂不强制，后续须经过青鸾单一前端 report-only 兼容验证再启用。
- 原因：高风险令牌没有恢复需求，持久化只会扩大 XSS、扩展和依赖污染后的暴露窗口；
  普通会话保留标签页刷新能力，但由数据库权威安全版本、refresh family 撤销和跨标签页
  清理共同限制生命周期。传输探针将文档性 HTTPS 要求变为可持续机器证据。
- 影响：前端登录/首次改密/注销/多标签页测试、会话 store、CSP、部署手册、OpenAPI
  与生产证书监控。真实浏览器仍须在每次生产发布后验证唯一入口无 CSP violation。

## D049 回调传输与敏感密文使用版本化上下文边界

- 决策：生产 callback 只接受 HTTPS，可选受控 CA/mTLS；每次投递重新解析 DNS 后固定
  已批准 IP，保留原 Host/SNI，禁止重定向。固定 IP 请求禁用 keep-alive，确保共享 IP 上
  每个逻辑主机重新完成证书/SNI 校验；连接并发、超时、响应头和正文仍有硬上限。
  callback task 创建时固化 event ID、secret 密文/密钥版本与签名协议版本。
- 密文边界：新 AES-GCM envelope 把 schema/key version、domain、table、column 与不可变
  object ID 编入规范 AAD；phone、短信正文、callback secret、vendor raw 与 export frame
  分域。SMSX2 导出帧另绑定 task、lease、frame index 和 data/terminal 类型，拒绝重排、
  删除、复制、跨文件移植和缺失终止帧。
- 兼容策略：读路径只在明确允许时双读旧通用 AAD/SMSX1，新写一律使用 v2；按
  `docs/context-encryption-migration.md` 在保留旧 key version 的窗口内逐批重加密，
  不落中间明文，旧格式归零且完成回滚观察后才关闭兼容读取或移除旧密钥。
- 影响：schema v1.6.35、0036 回调快照迁移、加密/发送/raw/callback/export 边界、
  OpenAPI、部署配置与密码学/真实证书链门禁。

## BLOCKED 模板

### BLOCKED-D019 T4.7 G0 被审批额度与网络沙箱阻断

- 解决状态：2026-07-12 执行权限恢复后原命令通过；Docker 29.4.0、npm registry、Node 20 容器依赖重建、生产依赖零漏洞、构建/类型/25 项前端测试及迁移双建库一致性均已验证，D019 已解除。

- 任务：M4/T4.7 生命周期、分区滚动与全仓回归门禁。
- 现象与完整错误：T4.7 的 20 项定向测试、真实 owner 分区维护、运行态 DML 清理及全量后端 407 项测试均通过（services coverage 83.51%）；提交前的迁移一致性命令被自动审批以 `You've hit your usage limit` 拒绝，Node 20 容器前端门禁随后因 Docker socket `permission denied` 无法启动，本机依赖重建又因 registry DNS `ENOTFOUND` 无法完成。
- 尝试 1（假设/动作/结果）：假设已批准的迁移检查可直接复验；原样申请受控执行 `uv run python scripts_support/check_migration.py`，审批系统因额度耗尽拒绝，并明确禁止绕过。
- 尝试 2（假设/动作/结果）：假设前端锁文件可由既有 Node 20 镜像独立验证；原样执行容器 G0，Docker API 权限被拒；转用现有本机依赖时确认其来自 Linux 容器，缺少 macOS ARM Rolldown 原生绑定，不能作为绿色证据。
- 尝试 3（假设/动作/结果）：经用户授权按 lockfile 重建依赖；首次暴露 npm 用户日志目录不可写，改用允许写入的 `/tmp` 缓存后取得明确根因：registry 请求持续 `getaddrinfo ENOTFOUND`，按环境故障上限停止重试。
- 是否位于 G2 关键路径：是；迁移一致性与干净 Node 20 前端构建/测试均由 `verify_all.sh` 强制执行。
- 影响面：T4.7 实现和后端证据已保留，但不得勾选完成、不得提交为功能完成，也不得越过串行协议进入 T4.8；ECharts 6.1.0 已在 package.json/package-lock.json 锁定，不是缺失项。
- 最小安全降级（如适用）：无。跳过双建库比对、改用 Node 22/残缺 node_modules 或降低 G0/G2 均会削弱权威门禁。
- 建议人工动作：恢复工作区审批额度及 Docker socket/registry 网络访问；随后在隔离工作树原样执行 `cd backend && UV_CACHE_DIR=/tmp/sms-platform-uv-cache uv run python scripts_support/check_migration.py`，再执行根目录 Node 20 容器前端完整门禁。两者全绿后将 T4.7 改为 `[x]`、解除 D019 并继续 T4.8。

### BLOCKED-D01 Alembic 单次执行整份 schema 与 asyncpg 不兼容

- 任务：M0/T0.4 schema、Alembic 基线与 DBA 权限验收。
- 现象与完整错误：SQLAlchemy 文本路径会把注释中的 JSON 冒号识别为绑定参数；原样 DDL 到达 asyncpg 后报 `cannot insert multiple commands into a prepared statement`。
- 尝试 1（假设/动作/结果）：假设 `op.execute(str)` 可执行全文；实际在发送数据库前报 `A value is required for bind parameter '2'`。
- 尝试 2（假设/动作/结果）：假设 SQLAlchemy `DDL` 可绕过绑定；它对 schema 中的百分号做模板替换，报 `unsupported format character`。
- 尝试 3（假设/动作/结果）：自定义原样 DDL 编译器，成功保持全文字符不变；asyncpg prepared statement 明确拒绝多条命令。
- 是否位于 G2 关键路径：是；数据库必须可从空库可靠建立。
- 影响面：无法同时满足“asyncpg”与“整文件只调用一次 op.execute”两个约束；审批系统额度不足还阻止了本会话通过 stdin 导入 `deploy/seed.example.sql`，静态 seed 文件未修改。
- 最小安全降级（如适用）：继续使用 asyncpg；首迁移从唯一 `schema.sql` 读取全文，以识别字符串、标识符、嵌套注释和 dollar quote 的内置解析器按顶层分号无损切分，在 Alembic 同一事务内逐条 `op.execute`。测试断言所有切片重新拼接与原文件逐字相等。全新 PostgreSQL 16 空库迁移成功，`sms_app` 非超级用户/无建库建角色权限，audit INSERT 成功且 UPDATE/DELETE 均被拒绝。
- 建议人工动作：若必须坚持单次 `op.execute`，批准 migrate 专用同步 PostgreSQL 驱动并重新评审锁定栈；工作区审批额度恢复后重跑 BOOTSTRAP 第 3 节 seed 导入命令。

### BLOCKED-D02 迁移一致性临时容器被工作区审批额度阻断

- 解决状态：2026-07-11 审批额度恢复后原命令通过，双空库表/列/索引/约束集合一致；seed-dev 容器集成与 0600 密钥文件验收通过，D02 已解除。

- 任务：M0/T0.6 机判资产与 seed-dev。
- 现象与完整错误：`uv run python scripts_support/check_migration.py` 需要创建临时 PostgreSQL 16 容器；沙箱内报 Docker API permission denied，按权限升级重试后审批系统返回 `workspace is out of credits`。
- 尝试 1（环境重试/结果）：在默认沙箱运行完整检查器，Docker socket 被拒绝；诊断增强后确认不是检查器 SQL 或容器参数错误。
- 尝试 2（环境重试/结果）：按沙箱规则申请受控升级执行同一命令，自动审批因工作区额度耗尽拒绝。
- 是否位于 G2 关键路径：是；`verify_all.sh` 必须比较 schema.sql 与 Alembic 的表/列/索引/约束集合。
- 影响面：检查器与 seed-dev 单元行为已实现且 6 项定向测试通过，但双空库真实比对尚无绿色证据；不得将 T0.6 或 M0 标为完成。
- 最小安全降级（如适用）：无。跳过迁移一致性会削弱 G2，改用其他 Docker 调用路径会绕过明确的审批拒绝，均被 AUTOPILOT 与执行政策禁止。
- 建议人工动作：为工作区补充审批额度；恢复后只需执行 `cd backend && uv run python scripts_support/check_migration.py`，通过后把 T0.6 改回未完成状态，继续 seed-dev 容器集成验收。

### BLOCKED-Dnn 标题

- 任务：
- 现象与完整错误：
- 尝试 1（假设/动作/结果）：
- 尝试 2（假设/动作/结果）：
- 尝试 3（假设/动作/结果）：
- 是否位于 G2 关键路径：
- 影响面：
- 最小安全降级（如适用）：
- 建议人工动作：

## ENV-D03 M1 G1 无法访问 Docker 执行环境

- 解决状态：2026-07-11 权限恢复后使用空闲宿主端口完整重跑通过；Compose、安全权限、迁移、契约、81.33% 覆盖率与 seed-dev 均绿色，D03 已解除。

- 任务：M1 `bash scripts/verify_milestone.sh M1` 权威门禁。
- 现象：门禁的规格检查、Ruff、Mypy 与 140 项后端测试全部通过；进入 Docker 前端门禁时，本机 Docker socket 返回 permission denied。
- 环境重试 1：在默认沙箱原样运行权威门禁，Docker API 拒绝访问。
- 环境重试 2：申请受控提权原样运行同一门禁，审批系统返回 `workspace is out of credits`，并明确禁止绕过或间接执行。
- 影响面：T1.10 已完成并经 G0 验收，但 M1 不得打 `m1` tag，也不得越过 G1 进入 M2。
- 恢复动作：工作区 owner 补充提权审批额度后，在隔离工作树原样执行 `UV_CACHE_DIR=/tmp/sms-platform-uv-cache VENDOR_MOCK=1 AUTH_MOCK=1 bash scripts/verify_milestone.sh M1`；全绿后更新 PROGRESS、打 tag `m1` 并进入 T2.1。

## D050 Redis 故障域采用三个独立托管端点

- 决策：生产把 Celery broker、认证/会话/step-up、配额/频控/幂等/业务锁拆为 broker、auth、control 三个不同托管高可用端点；不在应用层用数据库编号模拟隔离。
- 身份：三个端点分别使用 `sms_broker`、`sms_auth`、`sms_control` ACL 用户和独立 Docker secret，关闭 default 用户与危险管理命令。
- 最小挂载：API 只获得 auth/control，worker 只获得 broker/control，outbox-dispatcher只获得 broker；因此 API 容器被攻陷不能操纵 Celery，callback worker 不能读取或删除会话撤销/step-up。
- 故障语义：auth 不可用时认证与会话校验 fail closed；broker 故障由 PostgreSQL Outbox 保存事实；control 数据只作为 PostgreSQL 用量事实与业务事实的可重建投影。
- 取舍：development/test Compose 仍提供三个单节点实例以保持本地可复现，生产由 `REDIS_HA_MODE=managed` 强制使用托管高可用端点；不在本仓库自行维护 Sentinel 仲裁。

## D051 公有快照首次切换使用可恢复的职责拓扑 bootstrap

- 决策：无历史公有快照首次跨越旧单 Redis/`sms_app` 边界时，只有操作者明确确认后才允许已安装、目标 commit 绑定的 root-owned bootstrap 在服务器本机生成缺失的 11 个独立凭据。它备份旧权威 secrets 与根 `.env`，保留旧 runtime generation、PostgreSQL、全部 Docker volume 和运行态目录；先在旧 broker 旁启动 auth/control，正式 broker 与七职责角色只在快速更新 checkpoint 后切换。
- 失败语义：准备中任一步失败都停止新 Redis 域并恢复旧权威 secrets、旧 runtime target 与旧 Redis image 标签；新生成的失败现场和旧凭据备份均保留，禁止清库、删卷或自动初始化其他数据。apply 失败继续沿用迁移后的 fail-closed 规则，不做 schema 逆迁移。
- 原因：公有快照目标 Compose 已要求三域 Redis 与七个独立数据库角色，而旧测试机没有这些凭据和服务；只把路径加入 high-risk 分类会在 pause 前失去 control Redis，只先改 secrets 又会形成不可恢复的半切换。
- 影响：host-control 固定资产、快速更新 manager、公开快照 runbook 和服务器一次性操作。管理员、正式厂商 Key、测试号码与真实联调激活仍不属于此 bootstrap。

## D052 测试更新不再等待托管 CI

- 决策：公开主仓库继续运行 `ci-gate` 和按风险分类的 CI/G2，作为异步质量证据；`main`
  不配置 required check。开发者完成必要定向测试并推送不可变 commit 后，唯一快速更新
  入口不查询 GitHub Checks API、不等待 CI，也不在本地重复组件测试或 G2，直接执行镜像、
  expand-only 迁移和运行态验证。
- 原因：测试阶段需要尽快暴露真实服务器问题；把托管队列和完整 G2 同时设为合并、driver
  的串行前置，会重复等待并拖慢联调。目标 commit、同仓库身份、镜像摘要、数据库 checkpoint、
  host-control 快照、fail-closed 状态机和最终 `state=verified` 仍提供部署边界。
- 影响：本决策取代 D034、D040 中“`ci-gate` 是 required check 且 test_update 必须验证
  绿色结果”的部分；生产 Release Gate、最终 SHA 的完整质量/安全/G2 与四镜像扫描不变。

## D053 风险分级测试门禁与同树提升

- 决策：D052 的“所有测试更新均不验证托管 CI”由本决策取代。普通 `web-only` 和
  `backend-safe` 无迁移更新仍允许 CI 并行运行；high-risk、迁移或 host-control 更新在
  `apply` 前必须验证目标 commit 上由 GitHub Actions 应用产生的精确 `ci-gate=success`。
  `main` 继续以 `ci-gate` 为唯一 required check。PR 分支在测试环境完成 verified 与表面
  验收后 squash merge；若新 main 与已验证分支 tree 完全一致，则以 `promote` 只替换
  Git commit 身份，不重建相同镜像、不重跑迁移。
- 原因：低风险联调需要短反馈环，高风险边界不能把异步证据当作可选；squash 只改变提交
  身份时重复构建和部署不增加安全事实。精确 commit、GitHub Actions app 身份与 tree
  等价验证可同时消除同名状态伪造和无效重复工作。
- 影响：新增统一 `plan` / `build` / `apply` / `status` / `promote` 入口、GitHub `test`
  Environment Deployment 记录和本地严格解析的 `.env.test-update`。CI 在高风险 PR 中
  运行 integration 模式，并按路径加入性能/release-control（其中性能部分后由 D059
  取代）；人工、定时和生产候选仍保留完整 11 阶段。管理员初始化、正式 Key、测试号码、
  数据库与 volume 继续完全独立。

## D054 公开工作区禁止跨历史 Git 对象切换

- 决策：本决策取代 D051 中“通过日常快速更新完成公有快照首次切换”的入口设计。当前公开
  工作区不得添加私有归档 remote、fetch 私有 commit、创建私有 ref 或生成/上传私有 Git
  pack；`scripts/test_update.sh` 明确拒绝旧的 `--public-snapshot-cutover` 参数。服务器端
  已安装的兼容资产只为保留既有运行态恢复能力，不构成新的可执行授权。
- 迁移边界：无共同历史的测试服务器基线必须另立变更单，在不属于公开工作区的隔离临时
  证据仓库和受控维护窗口中完成。评审必须覆盖逐文件公开快照验真、PostgreSQL/volume
  保留与回退、24 件 secrets、三域 Redis、七职责数据库角色和证据清理；最终服务器
  HEAD/origin 直接绑定公开 commit，任何私有 URL、ref、commit 或对象不得回流公开工作区。
- 原因：即使最小 pack 不含完整提交历史，把私有来源 commit/tree 对象导入公开工作区仍会
  扩大误推送、对象残留和后续开发误用风险，也与 `PUBLICATION.md` 的无私有对象边界冲突。
- 影响：当前旧测试服务器基线不在公开对象库时，日常 `plan/apply` 按设计失败关闭；完成
  单独基线迁移前不得用 raw Git、Compose、临时 ref 或旧参数绕过。正常同历史更新和
  `promote`、数据库/volume 保留、真实联调控制面均不变。

## D055 青鸾成为根路径唯一前端

- 决策：开发测试阶段结束经典版与青鸾版双 SPA 并存。删除 `frontend/legacy`，将青鸾版
  从 `/next/` 提升到唯一根路径 `/`；Web 镜像只包含一份 `dist/`，Nginx 对开发阶段退役
  的 `/next` 入口返回 `410 Gone`，不保留版本切换按钮、隐藏开关或第二份静态产物。
- 原因：项目尚未上线，不存在需要维护的外部旧入口；两套独立源码、测试和构建已经产生
  安全能力漂移。青鸾版包含正式凭据 Secure Context 检查、真实联调重置和 correlation
  展示等较新的运行能力，继续双写只会扩大回归面。
- 设计基准：本决策同时以深色青鸾 Console 取代原浅色经典视觉基准；`docs/ui-design.md`、
  `docs/sms-ui-prototype.html`、`frontend/src` 与结构保真测试共同描述唯一设计，不再以
  `legacy` 实现反向约束产品。
- 影响：前端构建、Dockerfile、Nginx、Compose 健康检查、部署手册和合同测试收敛为一套。
  API、OpenAPI、数据库、Alembic 和浏览器会话键不变；失败时只回退整个上一版 Web 镜像，
  不在新镜像内恢复双前端。

## D056 Owner PR 在精确 push CI 成功后自动合并

- 决策：owner 的同仓非 `main` 分支继续自动创建 Draft PR；对应 `.github/workflows/ci.yml`
  push run 成功后，独立 `workflow_run` 校验 workflow 路径、事件、actor、head repository、
  branch 和 head SHA，再把唯一匹配的开放 PR 改为 Ready 并请求 squash auto-merge。
- 安全边界：自动化不使用 `--admin`，不自行伪造 check，也不处理 fork、人工、定时、失败、
  cancelled、`main` 或 SHA 已漂移的 run。required `ci-gate`、会话解决、分支保护和冲突仍由
  GitHub 决定是否真正合并；任何匹配歧义或字段不一致均失败关闭。
- 交付语义：自动合并只表示仓库集成完成，不表示测试服务器 `state=verified`。默认对合并
  后的精确 `origin/main` 执行 `plan` / `apply` / `status`；若分支已提前验证且 tree 相同，
  仍可使用 `promote`。本决策取代 D053 中“必须先完成分支环境验收再合并”的默认顺序，
  但不放宽 high-risk、迁移或控制面更新在 `apply` 前的精确 `ci-gate` 要求。
- 效率约束：自动合并消除人工转 Ready 和点击合并；只有 squash merge tree 与 PR head
  tree 完全一致，且原 head 的精确 `ci-gate` 仍为 GitHub Actions success 时，才把该证据
  复用于 merge SHA，避免重复 G2。开发默认从最新 `main` 创建非堆叠分支，避免父 PR
  squash 后对子 PR 重放。
- 落后处理：自动合并前先读取 PR 的 `mergeStateStatus`；`BEHIND` 时明确失败关闭并提示
  把 base main 合入 PR 分支后重新推送（`GITHUB_TOKEN` 产生的分支更新不会触发 push CI，
  自动化无法在内部自证新 head）；`DIRTY` 时同样明确失败并保留 PR 供人工处理，避免
  `--auto` 在严格分支保护下无限排队直到 12 分钟超时。

## D057 维护期开发与测试部署解耦

- 决策：`MAINTENANCE.md` 是唯一日常流程入口；建设期任务清单和分阶段门禁入口退出后
  已从工作树删除，历史通过 Git 查阅。编码循环运行定向测试，提交前才运行
  `scripts/dev_check.sh --changed`，普通维护工作不再默认绑定测试服务器。
- 元数据分类：`test_update_contract.py` 把 `.github/**` 与受信任的操作文档视为无运行时
  变更，因此包含工作流/文档提交的 main 不再阻塞后续快速更新的差异分类；`.github/` 的
  CI 门禁仍由 `classify_ci_changes.py` 按 G2 全量执行，未放宽受保护变更约束。
- 测试部署：只有需要共享环境验收时，才默认对自动合并后的精确 `origin/main` 执行
  `scripts/test_update.sh apply --ref origin/main`。`apply` 已验证最终 `state=verified`；
  `plan` 和独立 `status` 分别降为可选预览与后续诊断。分支部署仅作为明确例外。
- 门禁边界：受保护变更的精确 `ci-gate`、G2、迁移 checkpoint、失败关闭、应用镜像回退、
  生产 Release Gate、Trivy/SBOM/镜像身份和所有数据安全规则均不变；只移除重复执行与历史
  文案耦合。

## D058 合并提交主动验真并精确清理短期分支

- 问题：使用仓库 `GITHUB_TOKEN` 完成 squash merge 时，GitHub 不会再由该 token 产生的
  普通 `push` 事件启动新 workflow；仓库的自动删分支设置也未在实测中删除 owner 分支。
  因而 PR head 已有绿色证据，但 merge SHA 缺少 `ci-gate`，远端短期分支仍需人工清理。
- 决策：owner PR 合并后先重新校验 PR number、base、head SHA、同仓身份和 merge SHA，
  再创建只含 merge SHA 与 workflow run ID 的一次性 tag，通过允许由 `GITHUB_TOKEN`
  触发的 `workflow_dispatch` 启动现有 CI。内部三个输入必须全有或全无，tag、事件
  `GITHUB_SHA`、merge commit、PR head 和原 head `ci-gate` 逐项绑定；普通人工 dispatch
  不带内部输入，仍执行完整 CI。
- 复用与降级：merge tree 与 PR head tree 相同且原精确 `ci-gate` 成功时，只运行稳定规格
  检查并在 merge SHA 生成新的 `ci-gate`，不重复组件测试或 G2；证据缺失、API 暂时不可用
  或 tree 不同则在 merge SHA 完整运行，绝不把“无法验证”当作成功。一次性 tag 在 dispatch
  后以精确 lease 删除。
- 分支清理：PR 确认已合并并成功派发主干验真后，查询远端 head ref；已由 GitHub 删除视为
  幂等成功，否则只有该 ref 仍精确等于原 CI head SHA 时才以 `--force-with-lease` 删除。
  若分支并发前移则删除失败并保留新提交。每个分支的自动化互斥，跨分支合并依靠唯一 PR、
  head/merge SHA、tree 与临时 tag 独立绑定，不使用 PAT、`--admin` 或无条件 REST DELETE。

## D059 性能冒烟退出 PR 阻塞链

- 决策：本决策取代 D053 中“高风险 PR 按路径加入性能门禁”的部分。PR 仍执行组件门禁、
  G2 integration 和按路径选择的发布控制烟测，但不再运行性能冒烟；元数据不可靠时其余
  门禁仍按完整档失败关闭。
  每日定时、人工完整运行、需要失败关闭为完整档的 `main` 更新与生产候选继续执行默认
  时长、默认负载和原阈值的完整性能门禁。
- 原因：Hosted runner 最近五次性能阶段稳定消耗约 4 分 17 秒，而受理与 verify P95
  长期保有较大阈值余量；将其作为每个受保护 PR 的同步阻塞证据，反馈成本高于即时收益。
  性能门禁曾发现单 API 进程容量问题，且仍是 NFR-01 唯一自动整栈量化证据，因此只降低
  执行频率，不删除脚本、不缩短采样、不放宽阈值。
- 影响：PR 反馈缩短约 4 分钟；性能回归的发现窗口扩大到下一次成功的每日、人工或生产
  候选门禁。`perf_smoke.py`、`docs/PERFORMANCE.md`、NFR-01 追踪、本地按需完整复验和
  Release Gate 均保留。

## D060 无共同历史的测试服务器以一次性 public bundle 建立公开基线

- 决策：D054 所要求的独立跨历史变更采用一次性、目标 commit 绑定的 public bundle
  激活流程。隔离 checkout 必须只含规范公开仓库 `main` 的完整可达对象，目标 commit
  必须已有 GitHub Actions 精确 `ci-gate=success`；bundle 只发布
  `refs/heads/main`，不得含 prerequisite、额外 ref、不可达对象、私有 URL、私有 commit
  或私有对象。本地 driver 固定为 `prepare → build → finalize`：build 从稳定 bundle
  的原始 blob 逐路径物化私有规范 context，在每张镜像构建前后复验路径/mode/内容，并
  以固定 Docker argv 自行 build/inspect/save；finalize 不接收手工 image ID，而从同一
  冻结身份一次生成 manifest/request。API/Web 必须同时构建 `linux/amd64` 不可变镜像，
  并逐一绑定 image ID、archive SHA-256、`org.opencontainers.image.version`、
  `org.opencontainers.image.revision=target` 与
  `com.sms-platform.schema-revision=0039_manual_job_outbox`。服务器当前迁移头必须与
  目标相同，本流程不运行 Alembic、不创建 checkpoint，也不允许 expand 或 contract 迁移。
- 保留与切换：root-owned host-control 先用同一 public bundle 的临时 public ref 安装，
  随后固定
  `baseline-prepare/apply/verify/status/finalize/cleanup` 状态机复用 high-risk 暂停、
  不安全分片检查和既有服务验收。服务器 operator 身份固定为 UID/GID `1000:1000`，
  不从当前主机动态放宽；active root 固定为 `0:1000 2770`，`backend`/`deploy` 固定为
  `1000:1000 2770`。核心只把 `/opt/sms-platform` 作为目录交换边界，以 rename 保留并
  迁移旧 `.env`、`deploy/secrets`、`backend/.venv` 三项 allowlist；
  PostgreSQL、全部 Docker volume、三域 Redis、七职责数据库角色、运行态目录、正式厂商
  Key、加密测试号码、当日账本与审计事实均保持原位且不得重建、轮换、清除或摘要化。
  根目录使用同文件系统原子 exchange；API/Web、`vendor-control-agent` unit 或 verify
  任一步失败时，按旧 root、旧 unit、旧镜像/服务恢复。回退不跨 schema，不回 Mock，
  不清库、不删卷；回退不完整则保持两条发送 lane fail closed。
- 终态与证据：只有目标 root/origin/tree、API/Web 标签与服务、迁移头、账本、Redis/角色/
  volume 保留及正式联调安全投影逐项通过，test-update 状态才可成为 `verified`；随后
  `baseline-finalize` 只把不可变 journal 确认为 verified，继续保留旧 recovery root，
  同时保留旧镜像回退标签，不把“finalize”解释为删除恢复能力。只有随后表面验收全部通过，
  才运行 `baseline-cleanup`，以可重入 tombstone 删除旧 root，严格删除并复核旧镜像
  回退标签，并删除 incoming 中本次
  `public-baseline.bundle`、`api.tar`、`web.tar`；必须保留 manifest/request、标准
  test-update store 和 core journal。数据库、volume、secrets、正式 Key/号码、审计与
  当前运行镜像也不得作为 cleanup 对象。临时 public ref 以精确旧值删除，本地隔离构建
  材料只在确认本次临时目录后清理。完整执行合同见
  `docs/runbooks/public-baseline-activation.md`；该入口完成一次后退役，日常部署恢复
  `scripts/test_update.sh apply --ref origin/main`。

## D061 安全日报由主机采集器提供脱敏证据快照

- 决策：安全日报的主机证据由独立 `security-report-collector` timer 在 08:00 前聚合，
  只向 `security-report-control/incoming` 写入无原文、无 IP、无账号和无请求路径的
  结构化计数与覆盖状态。平台 API/worker 不读取主机日志；mailer 只消费已验证快照。
- 语义：全部证据源缺失时保持 `generation_status=unavailable`；仍有真实来源但存在缺口
  时生成 `attention` 并在 `coverage` 和处置项中明确缺口，禁止以示例 JSON 或零值伪造
  完整性。采集器写入失败不改变数据库已有 ready 事实。
- 原因：原实现只定义了“外部采集器交付”接口，测试主机没有安装采集器时页面只能看到
  `unavailable`，但缺少可执行的恢复路径。把采集、消费、投递继续隔离，既能补齐默认
  部署链路，也不会扩大 API 或 mailer 的主机权限。

## D062 应用来源 IP 白名单只作用于 API Key 认证路径

- 决策：`app.allowed_ips`（TEXT[]，规范化 CIDR，空数组=不限）作为应用级入站白名单，
  在 `core/apikey.py` 的 `require_api_app`/`optional_api_app` 认证依赖内统一强制：
  白名单非空且 `request.client.host` 未命中或不可解析时返回 `403 IP_NOT_ALLOWED`，
  且拒绝发生在路由处理之前，不消费应用限流与配额。Web 用户 JWT 路径不受影响。
- 原因：内部系统主要通过 X-Api-Key 接入，密钥泄露后 IP 白名单是最直接的止损边界；
  强制点放在唯一认证依赖内可避免散落 if-else，也满足“认证方式不得按路径猜测”的契约。
  IP 来源依赖既有可信代理契约（外部 TLS 终结器必须剥离并重写 XFF/X-Real-IP，
  内部 Nginx 仅透传、uvicorn `--forwarded-allow-ips` 固定 172.31.250.3），不需要新增
  环境变量或中间件；未来接 CDN/WAF 时必须同步调整外部代理头重写与部署契约测试。
- 影响：schema/migration、`core/apikey.py`、应用管理 API 与前端、openapi/PRD/AGENTS
  错误码表、单元/API/集成/E2E 与 UAT-29 用例；轮换后新旧 Key 受同一白名单约束。

## D063 同历史测试基线重对齐使用独立严格入口

- 决策：日常 `classify-nul` 与 `apply` 的禁止路径保持不变；只为“服务器 commit 是目标
  `origin/main` 的祖先、存在真实 expand 迁移前移”的旧测试基线提供一次性
  `rebaseline`。它只额外接受两个固定运行控制脚本和枚举的非运行态文件，要求两脚本同时
  出现在差异中，并强制 API/Web 双镜像与 high-risk 路径。性能门禁脚本及其权威说明
  `docs/PERFORMANCE.md` 只在该一次性入口按非运行态证据枚举，日常 `apply` 仍拒绝二者。
- 证据：目标 commit 必须已有 GitHub Actions 应用产生的 `backend`、`frontend`、
  `security`、`g2`、`ci-gate` 五项精确 success；`ci-gate` 单项成功不足以执行本入口。
  host-control 资产发生变化时，必须先按同一目标 commit 重装 root-owned 不可变快照。
- 双端验证：driver 必须把 `apply` 或 `rebaseline` 写入最后上传的不可变请求；服务器不信任
  本地分类结果，而是重新确认 `origin/main`、同历史祖先、API/Web、expand 迁移且无 cutover
  证据，再以独立的严格 rebaseline 分类器复验完整差异。旧请求缺少该字段时只按 `apply`
  处理，不能隐式获得一次性重基线权限。
- 运行边界：服务器既有状态机继续负责两条 lane 暂停、submitting/retrying/uncertain 拦截、
  AES-GCM 数据库 checkpoint、expand-only 迁移、应用镜像回退、verify 与 operator Git
  读路径复核。跨历史、无迁移、分支 ref、未知禁止路径或任一证据缺失均失败关闭；不清库、
  不删卷、不重建或轮换 secrets，不修改正式 Key、加密测试号码和真实联调控制状态。
- 原因：历史积累差异不能通过扩大日常白名单消解；把已知基线修复单独建模，既能完成测试
  环境追平，又让未来普通更新继续对运行凭据和发布控制改动保持拒绝默认。

## D064 主机快照从服务端临时 Git 仓库绑定目标 object

- 决策：一次性主机快照安装不得向活动 `/opt/sms-platform/.git` fetch，也不得临时给其
  group 写权限或改用 root Git。operator 从该 checkout 只读取得既有 `origin`，在服务器
  `mktemp` 目录初始化临时 bare 仓库并只 fetch 审核过的目标分支；目标 ref 必须解析为外部
  已绑定完整 CI 证据的 40 位 SHA，归档也只能从该临时仓库生成。
- 验真：root 安装器继续以 staged 文件逐字节对照目标 Git object；安装期间仅通过固定临时
  仓库的 objects 目录提供 `GIT_ALTERNATE_OBJECT_DIRECTORIES`，不把对象、ref 或 remote
  写回活动 checkout。安装完成或失败退出都删除临时 Git 仓库；上传的锁定 cloudflared
  二进制只在完整安装和 status 验证成功后删除。
- 可靠性：快速更新 manager 对同一个固定 source refspec 最多执行 3 次 fetch，失败间隔
  固定为 1/2 秒；每次 argv 完全相同，不改变 origin、ref 或 commit。全部失败时仍在发送
  暂停、checkpoint、迁移和应用切换前阻断，不能降级为客户端源码上传或手工 Git。
- 原因：受控更新会有意把活动 Git 元数据恢复为 operator group 只读，原安装命令却要求同一
  operator 在其中 fetch，合同互相矛盾。隔离服务端取证仓库既保留“源码不是客户端上传”的
  信任边界，也不扩大日常更新用户对活动 Git 元数据的权限。

## D065 性能客户端以有界 keep-alive 连接池和隔离状态测量 API 受理

- 决策：保持 API 两个 Uvicorn worker、Compose 资源上限、30 RPS 持续 60 秒和受理 P95
  `<300ms` 的 NFR-01 口径不变；性能客户端改为共享的 HTTPX 同步连接池，连接总数与
  keep-alive 连接均固定上限 64。完整 G2 在 E2E 后复用已构建镜像、重建开发卷、等待
  ready 并重新 seed，再执行性能阶段。请求体、响应解析、超时和失败关闭语义不变。
- 原因：原客户端通过 `urllib` 为 1800 个 localhost 请求逐次建立 TCP 连接，把 Hosted
  runner 的连接建立抖动计入应用受理 P95；连续失败为 343ms/354ms。把 API CPU 上限从
  1.5 核提高到 2 核的分支完整性能档反而达到 441ms，证明资源上限不是有效修复，因此该
  变更必须撤销。复用有界连接更接近真实内部 API 客户端的长连接行为，也不改变服务端
  并发、业务负载、采样时长或阈值。E2E 会按合同产生大量数据库事实，直接继承这些状态
  会把前序验收负载混入性能基线；只重建 G2 开发卷可隔离状态而不更换候选镜像。
- 影响：性能脚本运行环境继续使用后端锁定的 HTTPX 依赖；连接池由单个脚本进程共享并在
  退出时关闭。契约测试以真实本地 HTTP/1.1 server 证明连续请求复用同一连接，并约束
  性能阶段按 down/up/ready/seed 顺序重建且不重复构建镜像；仍必须先以人工完整 CI 默认
  性能档验证，短参数或 PR 选择性 G2 不构成交付证据。
