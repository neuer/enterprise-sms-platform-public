# DBA 手册 — 角色、审计与生命周期

## 角色边界

- `sms_owner`：PostgreSQL 初始化超级用户和所有对象 owner；仅 `migrate`、备份恢复和经审批的生命周期维护使用。
- 运行态按 `sms_auth`、`sms_accept`、`sms_send`、`sms_callback`、`sms_export`、
  `sms_scheduler`、`sms_metrics` 七个职责隔离；完整矩阵见
  [database-roles.md](database-roles.md)。
- owner 与七个运行密码分别存放在独立 Docker secret 文件；容器只读取 UID
  隔离的 0400 运行副本。不得出现在 URL、环境变量、日志或命令历史。

首次空卷由 init 脚本创建 NOLOGIN 占位角色，`db-role-provision` 设置独立 LOGIN
密码。已有数据库升级时，DBA 必须确认七角色属性与旧角色停用状态：

```sql
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole
FROM pg_roles
WHERE rolname IN (
  'sms_owner','sms_auth','sms_accept','sms_send','sms_callback',
  'sms_export','sms_scheduler','sms_metrics','sms_app'
);
```

七个运行角色不得拥有超级用户、建库或建角色能力；`sms_app` 必须 NOLOGIN 且无授权。

## 审计只增验证

分别 `SET ROLE` 到六个写角色执行 INSERT，再尝试 UPDATE/DELETE：

```sql
INSERT INTO audit_log(actor, action) VALUES ('dba-check', 'immutability_probe');
UPDATE audit_log SET action='forbidden' WHERE actor='dba-check';
DELETE FROM audit_log WHERE actor='dba-check';
```

INSERT 应成功，UPDATE/DELETE/TRUNCATE 必须报权限错误。`sms_metrics` 连 INSERT 也必须
失败。验证记录保留，不得为“清理测试”扩大任何运行角色权限。

## 默认权限

所有 Alembic 迁移由 sms_owner 执行。新增表默认不向任何运行角色授权；迁移必须在同一
版本按职责显式更新矩阵并补权限测试，禁止广泛 `ALL TABLES/ALL SEQUENCES` 授权。

## 数据库密码轮换

Docker secret 只在首次 initdb 用于设置角色密码；替换权威源文件不会自动修改 PostgreSQL 角色。轮换必须在维护窗严格按以下顺序执行：

目标文件可以是 `db_owner_password`，或 `db_auth_password`、`db_accept_password`、
`db_send_password`、`db_callback_password`、`db_export_password`、
`db_scheduler_password`、`db_metrics_password` 中的一个；禁止共享新值。

1. 在维护窗更新目标权威 secret 文件，并保持目录 0700、文件 0600；不得把密码作为
   环境变量、命令行参数、SQL 文本或 shell substitution。
2. 运行受控 secret generation 准备流程，再单独运行 `db-role-provision`；该服务通过
   `.pgpass` 和 psql 变量在容器内设置七个密码，不输出值。
3. 只重建需要该角色的运行服务；owner 轮换则重建 postgres 后执行一次 migrate。
4. 轮换后重新执行 `scripts/verify_database_roles.sh` 和连接健康检查；仅记录时间、
   文件名、mode、UID/GID 与结果，不记录值、长度、摘要或哈希。

对应的重建命令只传服务名，不传凭据：

```bash
# 运行角色：secret generation 准备成功后
sudo /usr/local/sbin/sms-compose up --no-deps --force-recreate db-role-provision
sudo /usr/local/sbin/sms-compose up -d --no-deps --force-recreate <受影响服务>

# db_owner_password：步骤 1、2 成功后
sudo /usr/local/sbin/sms-compose up -d --no-deps --force-recreate postgres
sudo /usr/local/sbin/sms-compose run --rm migrate
```

禁止把密码放进 `ALTER ROLE ... PASSWORD '...'` 命令行、shell 历史或工单正文。生产 Compose 操作唯一允许使用 `sudo /usr/local/sbin/sms-compose ...`，不得绕过包装器。
包装器对 `run` 只接受参数完全匹配的 `run --rm migrate`；任何其他服务、附加参数或缺少 `--rm` 都会在 Docker/Python 调用前退出 2。

## 36 个月审计保留

应用代码禁止 UPDATE/DELETE audit_log。保留期清理由 DBA 变更单驱动：先将超过 36 个月的记录导出到加密归档并校验行数与 SHA-256，再由 sms_owner 在维护窗执行删除；SQL、审批单、归档校验结果写入运维记录。不得把 owner 密码交给 beat 或 housekeeping。

## 消息与回复月分区滚动

所有运行角色都不具备 DDL 权限，Celery housekeeping 也不得创建或删除分区。`migrate`
服务在每次 `alembic upgrade head` 后自动运行 `scripts_support/maintain_partitions.py`；
独立的 `sms-partition-maintenance.timer` 还会每天通过同一 `migrate/sms_owner` 边界刷新窗口，
因此即使长期没有发布也不会耗尽预建分区。脚本按 Asia/Shanghai 创建当前至未来 13 个月的
`sms_message_YYYY_MM` / `sms_reply_YYYY_MM`，并按 `msg_retention_months` 删除过期分区。

首次启用定时器前先做只读计划核对，再执行一次：

```bash
sudo /usr/local/sbin/sms-compose partition-maintenance --dry-run
sudo /usr/local/sbin/sms-compose partition-maintenance
sudo systemctl enable --now sms-partition-maintenance.timer
```

维护事务使用固定 advisory lock；并发调用中只有一个执行，其他调用不做 DDL。父表、子表、
schema、规范分区名和实际 RANGE bound 必须完全匹配才允许操作；配置保留期超出 1–120
个月会失败关闭。实际执行只删除严格早于保留边界的月分区，并向 `audit_log` 写入不含标识符
和 PII 的创建数、删除数及窗口；失败最多退避重试三次，仍失败由 systemd journal 的
`status=failed` 触发运维告警。

执行后检查 `partition maintenance complete` 和审计记录，并查询 `pg_inherits` 确认两个
父表均覆盖未来 13 个月。`/readyz` 会独立要求当前及未来至少三个月齐备；缺失时实例不接流，
发布门禁也失败。禁止把分区脚本改为由 api、worker 或 beat 使用 owner 密码运行。

## 备份恢复

备份恢复的唯一执行手册为 [backup-restore.md](backup-restore.md)。备份必须使用 sms_owner，以 `pg_dump | openssl` 流式 AES 加密写入 `var/backups/`，禁止生成明文 dump；密文须 chmod 600、生成 SHA-256 并完成隔离库恢复。恢复后先运行迁移一致性、角色属性与 audit_log INSERT=true/UPDATE=false/DELETE=false 检查，再允许 api/worker/beat 启动。
