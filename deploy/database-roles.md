# PostgreSQL 运行角色矩阵

## 边界

`sms_owner` 只供 Alembic、分区维护、备份和恢复使用。运行容器不得挂载 owner
凭据。历史 `sms_app` 在迁移 `0034_database_role_matrix` 后永久为 `NOLOGIN`，且
没有 schema、table 或 sequence 权限；回滚不会恢复它。

七个运行角色均为 `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
NOREPLICATION`：

| 角色 | 调用方 | 允许范围 |
|---|---|---|
| `sms_auth` | API 认证、账号与 Provider 仓储 | 账号、身份、Provider、凭据、角色映射；只读应用 Key |
| `sms_accept` | API 消息受理与管理接口 | 批次、导入、审批、模板、签名、策略和受控联调受理 |
| `sms_send` | realtime/bulk worker | 分片、消息、厂商事实、用量账本、发送恢复、统计、异步导入、生命周期清理，以及签名/模板厂商绑定结果更新 |
| `sms_callback` | callback worker 与回调管理接口 | 回调事件、任务、租约、相关 Outbox 和告警 |
| `sms_export` | 导出 API 与 bulk export worker | 固化范围内的消息/回执读取和导出任务租约 |
| `sms_scheduler` | beat 与 Outbox dispatcher | job 追踪、Outbox 投递和调度告警 |
| `sms_metrics` | `/metrics` 聚合 | 迁移 0035 指定的非敏感列级 `SELECT`，无批次号、自定义 ID、手机号、正文或任何写权限 |

所有表、列、序列和操作都在 `schema.sql` 与迁移 0034/0035/0040/0054/0061 中显式列出。`sms_owner` 的
default privileges 明确不向运行角色授权，因此新增表或序列必须在同一迁移中更新
矩阵；不得使用 `GRANT ... ON ALL TABLES` 或 `ON ALL SEQUENCES` 给运行角色补权限。

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
3. `migrate` 只挂载 owner secret，执行 Alembic 和分区维护；
4. 各运行服务只挂载其实际需要的角色 secret。

应用 DSN 使用 SQLAlchemy `URL.create`。固定用户名与固定 secret 文件映射由
`Settings.database_url_for()` 提供，调用方不得传入任意用户名或密码路径。

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
