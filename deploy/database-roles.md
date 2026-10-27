# PostgreSQL 运行角色矩阵

## 边界

`sms_owner` 只供 Alembic、分区维护、备份和恢复使用。运行容器不得挂载 owner
凭据。历史 `sms_app` 在迁移 `0034_database_role_matrix` 后永久为 `NOLOGIN`，且
没有 schema、table 或 sequence 权限；回滚不会恢复它。

七个运行角色均为 `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
NOREPLICATION`：

| 角色 | 调用方 | 允许范围 |
|---|---|---|
| `sms_auth` | API 认证、账号与 Provider 仓储 | 账号、身份、Provider、凭据、角色映射、锁定/封禁审计；只读应用 Key |
| `sms_accept` | API 消息受理与管理接口 | 批次、导入、审批、模板、签名、策略和受控联调受理 |
| `sms_send` | realtime/report/bulk worker | 分片、消息、厂商事实、用量账本、发送恢复、统计、异步导入、生命周期清理，以及签名/模板厂商绑定结果更新 |
| `sms_callback` | callback worker 与回调管理接口 | 回调事件、任务、租约、相关 Outbox 和告警 |
| `sms_export` | 导出 API 与 bulk export worker | 固化范围内的消息/回执读取和导出任务租约 |
| `sms_scheduler` | beat 与 Outbox dispatcher | job 追踪、Outbox 投递和调度告警 |
| `sms_metrics` | `/metrics` 聚合 | 列级只读 `SELECT`（以 `schema.sql` GRANT 段为权威；metrics 列级授权在 0037/0038，不是 0035；0104 补 `sms_uncertain_resolution.source_channel/effect_error` 与 `sms_uncertain_child.recovered`；0117 补人工处置完成时间和 Outbox 延期判定所需的事件类型、受控错误码及下次执行时间），无批次号、自定义 ID、手机号、正文或任何写权限 |

运行角色授权以 `schema.sql` 末尾 GRANT 段为权威。历史迁移会把授权演进到该基线；含授权变更的迁移包括 0034/0037/0038/0040/0054/0061 等。`sms_owner` 的
default privileges 明确不向运行角色授权，因此新增表或序列必须在同一迁移中更新
矩阵并回写 schema.sql GRANT 段；不得使用 `GRANT ... ON ALL TABLES` 或 `ON ALL SEQUENCES` 给运行角色补权限。

`sms_export` 对 `export_task` 的 UPDATE 只覆盖租约、运行状态、完成文件路径、行数与时间；
不得更新 `public_id/decrypted/filters/scope_dept/scope_resolved` 或创建者稳定主体字段。
下载入口仍必须把授权 task id 与密文文件名/AAD 绑定，数据库角色收窄不是文件身份校验的替代。

`sms_send` 对异步导入只允许读取并更新/删除 `import_task`、读取并插入/删除
`import_phone`，另有 `user_account SELECT` 用于固化导入完成审计；对生命周期清理只补充
`idempotency_record`、终态 `callback_task` 和无主引用 `callback_report_event` 的删除权限。
导入任务 INSERT、导入号码 UPDATE 及审计 UPDATE/DELETE 均明确禁止。

签名和模板的创建、正文修改及删除仍只属于 `sms_accept`。`sms_send` 仅取得列级 UPDATE：
模板的厂商引用、审核状态、拒绝原因和更新时间，以及签名的厂商引用、审核状态和拒绝原因；
用于 realtime worker 把 Outbox 中的稳定主键提交给厂商后写回结果。它不能修改模板正文、变量声明、
部门或签名名称，也没有这两张表的表级 UPDATE、INSERT、DELETE 或 TRUNCATE 权限。
Bind Outbox 事件只自动尝试一次：超时或 worker 丢失时进入 dead-letter，必须先在厂商侧核对是否已受理，
再由受控运维流程决定是否重试，避免网络结果未知时自动创建重复签名或模板。

除纯只读 `sms_metrics` 外，需要写审计的六个角色只拥有 `audit_log INSERT`；
所有运行角色在 `audit_log` 上都没有 `UPDATE`、`DELETE` 或 `TRUNCATE`。callback 角色没有账号、
Provider、应用 Key 或 `sys_config` 写权限。

安全日报生成只读视图 `security_daily_audit_evidence` 仅暴露
`created_at/actor/actor_subject_kind/role/ip/action/object_type/object_id`
非载荷列，`sms_send` 与 `sms_accept` 持有该视图 SELECT，用于生成脱敏审计摘要；
审计主表的 `before_val/after_val` 载荷仍不对任何运行 worker 开放。

历史人工重发核验复用视图 `uncertain_resend_creation_evidence`（迁移 0110），仅向
`sms_send` 暴露带 `uncertain_resend=true` 标记的内部 `message_send` 审计的
`actor/object_id`。恢复仍需核对原请求指纹、完整批准上下文和子批次来源；视图不暴露审计载荷。
同一内部受理链路补充 `idempotency_claim` 的 SELECT/INSERT/UPDATE、
`idempotency_record` 的 INSERT 和对应两个序列权限；不授予 Claim DELETE/TRUNCATE。
内部重发审计沿用 realtime worker 已挂载的签名域，仅允许 `sms_send/realtime` 的
`system_resend:<resolution_id>` 写 `message_send`。数据库同时核验可信 child 关系、
批准状态及 generation；原有审计 HMAC 与不可修改权限保持生效。

自动日报任务运行在 bulk worker（`sms_send`），因此 `sms_send` 除
`security_daily_report` 外，还持有 `security_daily_delivery_request` 的
SELECT/INSERT/UPDATE（迁移 0041 起报告表、0051 起投递请求表），用于生成记录并
提交自动投递；人工生成与投递仍由 API 的 `sms_accept` 身份执行。

运行连接固定 `search_path=pg_catalog,public`。provision 同时撤销数据库的 PUBLIC
`TEMPORARY` 与 `CONNECT`，public schema 的 PUBLIC `CREATE` 也被撤销，避免临时对象或
同名对象劫持高风险未限定名写入。

## 凭据与启动顺序

权威 secret 文件为 `db_owner_password` 和七个 `db_<role>_password`。每个文件
0600 存放在仓库外的受控源目录，经 `prepare_runtime_secrets.py` 生成 UID 隔离的
0400 副本。值不得进入环境变量、DSN 文本、日志、工单或命令参数。

Compose 启动顺序固定为：

1. `postgres` 只挂载 owner secret，init 脚本创建 NOLOGIN 占位角色；
2. `db-role-provision` 挂载 PostgreSQL UID 副本，通过 `.pgpass` 连接，设置七个独立
   密码和 LOGIN，并撤销数据库的 PUBLIC CONNECT；
3. `migrate` 挂载 owner secret，并仅额外挂载数据 AES/HMAC keyring；0063 使用它们在
   同一事务内把历史短信/回复展示正文前滚为上下文密文。迁移容器不得获得厂商、JWT、
   LDAP、Redis 或运行数据库角色凭据；
4. 各运行服务只挂载其实际需要的角色 secret。

应用 DSN 使用 SQLAlchemy `URL.create`。固定用户名与固定 secret 文件映射由
`Settings.database_url_for()` 提供，调用方不得传入任意用户名或密码路径。

API 容器的默认 `DB_RUNTIME_ROLE=accept`，但认证子系统必须按子职责显式使用
`database_url_for("auth")`：锁定/IP 封禁审计、Transition 死信与认证阈值快照走
`sms_auth`，不得把这些写入扩大到 `sms_accept`。`sms_auth` 对
`auth_transition_dead_letter` 只有 INSERT（加序列 USAGE/SELECT），没有表级
SELECT/UPDATE/DELETE。生产缺失 `db_auth_password` 时启动/readyz 失败关闭。

## 验证与回滚

开发测试门禁运行 `scripts/verify_database_roles.sh`；迁移门禁还会在一次性
PostgreSQL 16 中验证：

- schema.sql 与完整 Alembic 建库结构一致；
- callback 修改账号/Provider/应用/配置被拒绝；
- metrics 写入、读取 `sms_batch.batch_no` 或 `sms_chunk.custom_id` 被拒绝；
- audit 非 INSERT 操作被拒绝；
- owner 新建的未来表没有自动授权；
- `sms_app` 保持 NOLOGIN 且无权限。

隔离恢复演练重复角色属性、audit 权限和 `sms_accept` 基础只读查询。若 0034
需要回滚，downgrade 只撤销七角色权限并保持 `sms_app NOLOGIN`，系统 fail closed；
必须以前滚迁移恢复明确矩阵，禁止临时恢复旧广权限账号。

内部重发与发送查询共享不含凭据的应用策略投影。`sms_send` 仅具有 `app` 业务列的 SELECT；API Key 摘要、前缀、算法、pepper 版本及轮换信息不可读（迁移 0114）。

幂等生命周期锁（迁移 0115）：回调角色仍无 `sms_batch` UPDATE。人工重推仅可调用
`lock_callback_idempotency_batch(task_id)` 锁定该任务已关联的批次；保护状态触发器
使用固定 `pg_catalog` 搜索路径与限定表名取得同一批次锁。两函数均撤销 PUBLIC 执行权，
仅前者向 `sms_callback` 开放 EXECUTE；函数不更新业务字段、不读出批次数据。

`role_mapping_invalidation` 是每个 Provider 一行的事务去重事实，七个运行角色均无直接权限。固定 search_path 的 owner 触发函数仅使用实际 OLD/NEW Provider 与数据库事务编号，在映射发生实质变更时使关联账号会话失效；不得用客户端参数或可写配置跳过该失效。

### 发送数据库失败观测

`send_db_failure_event` 仅包含随机事件标识、固定 phase/reason 和时间。
`sms_send` 仅有 INSERT，`sms_metrics` 仅有 phase/reason 列 SELECT；运行角色没有修改、删除权限。
`sms_send_db_failures` 是已保存事件的聚合计数，数据库故障期间无法保存的事件以
`send_db_observation_unavailable` 日志标记缺口，不能用该计数证明没有失败。
结果落库耗尽重试或其他失败使用 `send_db_failure` 告警；详情不含 SQL、参数或厂商响应。

0128 将过期 `worker_lease_event`、`send_db_failure_event` 明细压缩到各自的
`*_rollup` 固定维度累计表。七个运行角色仍无明细 UPDATE/DELETE/TRUNCATE 或累计表写权限；
仅 `sms_send` 能调用 `compact_runtime_events(text,timestamptz,integer)`，由固定
`search_path=pg_catalog`、限定 public 表名的 owner 函数执行有界删除与同事务累加。
PUBLIC 没有 EXECUTE，`sms_metrics` 仅获得累计维度和计数列 SELECT。
不得把这一观测专用入口用于审计、业务事实或其他表；详见
[运行观测事件保留](../docs/runbooks/runtime-event-retention.md)。

0129 只给 `sms_accept` 补人工 RAW 重放现有语句已经写入的列，不把受理角色扩成
`sms_send`。`raw_vendor_log` 新增 `UPDATE(custom_ids,item_count)`，租约 CAS 仍用
已有的 lease 列；不授予 payload 密文、指纹、`source` 或
`system_replay_audit_state`。`sms_chunk` 只有 `UPDATE(late_evidence_at)`，供合法
迟到报告证据和 `SELECT … FOR UPDATE` 行锁，没有 status、id、发送尝试或路由列，
也没有表级 UPDATE。`report_event`、`reply_event`、`sms_reply`、`unmatched_report`、
`report_event_projection`、`callback_report_event` 按当前 INSERT 列授予 INSERT，
事件事实没有 UPDATE/DELETE。`report_event_projection` 另有
`UPDATE(projection_changed)`；`callback_task` 另有
`UPDATE(message_ids,message_times,event_keys)`，不包含 url、密钥、投递状态或重试列。
序列只增加 `sms_reply_id_seq` 与 `unmatched_report_id_seq` 的 USAGE/SELECT。
匹配报告读取 `sms_vendor_attempt(vendor_id,chunk_id,outcome)`，该表没有写入权限。
`sms_chunk` 的这一列授权同时让 unknown 处置沿用既有 chunk→batch→resolution
锁序；它不授予 chunk.status，也不能代替处置审计。

0124 为 `sms_accept`、`sms_export` 授予受限函数
`lock_business_authorization(bigint,bigint,bigint,text,text,text,text,text)` 的 EXECUTE。
该 SECURITY DEFINER 函数固定 `search_path=pg_catalog`，所有业务表显式使用 public，
撤销 PUBLIC 执行权限；仅验证当前主体并在调用者业务事务中加锁、返回布尔结果。
不授予身份表直接 SELECT/INSERT/UPDATE/DELETE，不返回凭据或身份记录。
授权锁随业务事务一起提交或回滚，不能以另一个 auth 连接的生存状态代替此保证。

0131 为重发审批增加只读视图 `uncertain_resend_approval_applicant`，仅向 `sms_send`
暴露同一 resolution/generation 的确认人账号、身份和事件时登录名；不开放审计载荷或
`auth_identity` 表。待审批子批次的申请人取唯一确认事实，系统发送审计仍归 system，
保留按稳定账号回避自审批；旧决议缺失可信确认审计时失败关闭。
同一迁移仅允许 realtime 的 `system_resend:<id>` 写 `sensitive_hit`，数据库核对
批准状态、generation、来源分片/批次与两个有效管理员；不扩大普通 API 或其他系统动作。

0134 增加 `callback_batch_event`。`sms_accept` 与 `sms_send` 在原终态入队事务里追加快照，
`sms_callback` 只为缺少可信快照的旧任务追加一条新事件。三个角色都没有 UPDATE。
`sms_send` 保留 DELETE，供终态 `callback_task` 清理时级联删除快照；未终结任务和仍被
补发事件引用的原任务不进入清理。触发器拒绝任何 UPDATE。快照不含手机号、正文或回调 body。
