# PostgreSQL 加密备份与恢复手册

## 适用范围与职责

本手册适用于主节点日常全量备份、发布前回退点和隔离恢复演练。仅 DBA 可使用 `sms_owner` 执行；七个运行角色均不得备份、恢复或创建数据库。数据库内手机号虽已加密，备份仍属于高敏资产，必须二次加密、0600 保存、限权传输并按审批周期销毁。

备份口令不是平台运行 secrets 清单的一部分，必须由备份/KMS 系统单独托管。命令只允许通过仓库外 0600 文件路径 `BACKUP_PASSPHRASE_FILE` 读取；禁止把口令值放进环境变量、命令参数、`.env`、工单或 shell history。

## 自动备份、随机恢复与证据账本

生产默认使用 `lifecycle_manager.py` 复用本手册的流式加密备份和隔离恢复实现：

- `sms-backup.timer` 每日生成 AES-256-CBC/PBKDF2 密文快照，逐文件核对大小与 SHA-256；
- `sms-restore-drill.timer` 每周从所有完整性已验证快照中随机选择一份，恢复到唯一
  `sms_drill_*` 隔离数据库；不启动或改指 api、worker、beat；
- `sms-lifecycle-status.timer` 每小时核对最后备份、最后恢复演练、备份年龄、恢复耗时、
  数据缺口、保留边界和可用快照数；
- 三类失败都向 journal 输出固定 `event=lifecycle_alert`、操作名和异常类型，不输出路径、
  命令错误、手机号、密钥或其他 PII；service 以 5 分钟间隔最多重试三次。

完整性校验成功的新增快照仍是 `available=false`。只有流式解密恢复、Alembic 升级、关键
表/列、七角色权限、旧角色停用、只读业务查询及 RTO 全部通过后，该快照才会在
`/var/lib/sms-platform/backups/lifecycle-state.json` 标记为可用。任一后续演练失败会立刻
撤销该快照的可用标志；不得根据 `current` 符号链接或“文件存在”推断可恢复。

账本和报告均以 0600 原子替换，不保存备份路径、口令、密文内容或业务数据。默认保留 35
天且至少保留两份；清理只接受固定 snapshot ID、只删除保留边界外且不是 `current` 的目录，
并在账本记录边界、删除数与保留数。安装与启用命令见 [README.md](README.md)。

## 备份前检查

1. 记录 Git commit、Alembic version、PostgreSQL 16 小版本、执行人、目标保留期和变更单号。
2. 确认 `var/backups/` 被 Git 忽略且宿主剩余空间大于数据库已用空间的 1.5 倍。
3. 确认口令文件存在、权限 0600，且不在仓库目录。
4. 对发布回退点暂停会产生 schema 变更的操作；日常在线 `pg_dump` 不需要停止业务。

只读预检：

```bash
test -n "${BACKUP_PASSPHRASE_FILE:-}"
test -f "$BACKUP_PASSPHRASE_FILE"
test ! -L "$BACKUP_PASSPHRASE_FILE"
sudo /usr/local/sbin/sms-compose exec -T postgres \
  psql -U sms_owner -d sms -Atc "SELECT version_num FROM alembic_version"
```

## 流式加密全量备份

必须使用 Bash 并开启 `pipefail`。`pg_dump` 标准输出直接进入 OpenSSL，禁止先写任何明文 dump、临时 SQL 或未加密 tar 文件。OpenSSL `enc` 不支持 AES-GCM，因此这里使用 PBKDF2 + AES-256-CBC，并把密文 SHA-256 存入受控不可变变更记录；只有同时校验密文哈希和解密/恢复成功才算有效备份。

```bash
set -euo pipefail
umask 077
install -d -m 0700 var/backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="var/backups/sms_${stamp}.dump.enc"

sudo /usr/local/sbin/sms-compose exec -T postgres \
  pg_dump -U sms_owner -d sms --format=custom --compress=6 --no-owner \
| openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -pass file:"$BACKUP_PASSPHRASE_FILE" \
> "$backup"

test -s "$backup"
chmod 600 "$backup"
shasum -a 256 "$backup" > "${backup}.sha256"
chmod 600 "${backup}.sha256"
```

不要给 `pg_dump` 增加 `--no-acl`：恢复必须保留七角色显式矩阵与 audit_log 只增权限。命令退出 0 后，将 `.sha256` 内容和对象存储版本号写入不可变备份台账，但不得附备份口令或密文内容。

建议同时记录不含 PII 的核对数据：

```bash
sudo /usr/local/sbin/sms-compose exec -T postgres \
  psql -U sms_owner -d sms -Atc \
  "SELECT 'sms_batch='||count(*) FROM sms_batch UNION ALL
   SELECT 'audit_log='||count(*) FROM audit_log UNION ALL
   SELECT 'raw_vendor_log='||count(*) FROM raw_vendor_log"
```

## 密文与可恢复性校验

仅计算出哈希不代表可恢复。自动流程每周随机演练；每次发布回退点和重大恢复前还必须按需
恢复到隔离库：

```bash
set -euo pipefail
shasum -a 256 -c "${backup}.sha256"

sudo /usr/local/sbin/sms-compose exec -T postgres \
  dropdb -U sms_owner --if-exists sms_restore
sudo /usr/local/sbin/sms-compose exec -T postgres \
  createdb -U sms_owner -T template0 sms_restore

openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
  -pass file:"$BACKUP_PASSPHRASE_FILE" -in "$backup" \
| sudo /usr/local/sbin/sms-compose exec -T postgres \
    pg_restore -U sms_owner -d sms_restore --no-owner --exit-on-error
```

恢复过程中不得把解密 stdout 重定向到文件。任一命令失败，立即保留日志摘要、删除不完整隔离库并标记该备份不可用：

```bash
sudo /usr/local/sbin/sms-compose exec -T postgres \
  dropdb -U sms_owner --if-exists sms_restore
```

## 隔离恢复验收

先核对版本、关键表/列与关键表计数，再检查角色及 audit 权限，并以 `sms_accept` 执行固定
只读业务查询。计数须与备份台账一致；允许解释并记录在线备份快照时点造成的差异，不允许
静默忽略。

```bash
sudo /usr/local/sbin/sms-compose exec -T postgres \
  psql -U sms_owner -d sms_restore -v ON_ERROR_STOP=1 -Atc \
  "SELECT version_num FROM alembic_version;
   SELECT 'sms_batch='||count(*) FROM sms_batch;
   SELECT 'audit_log='||count(*) FROM audit_log;
   SELECT 'raw_vendor_log='||count(*) FROM raw_vendor_log;"

sudo /usr/local/sbin/sms-compose exec -T postgres \
  psql -U sms_owner -d sms_restore -v ON_ERROR_STOP=1 -Atc \
  "SELECT count(*), bool_and(NOT rolsuper AND NOT rolcreatedb
     AND NOT rolcreaterole AND NOT rolreplication AND NOT rolinherit)
     FROM pg_roles WHERE rolname IN
     ('sms_auth','sms_accept','sms_send','sms_callback',
      'sms_export','sms_scheduler','sms_metrics');
   SELECT has_table_privilege('sms_accept','audit_log','INSERT'),
          has_table_privilege('sms_accept','audit_log','UPDATE'),
          has_table_privilege('sms_accept','audit_log','DELETE'),
          has_table_privilege('sms_accept','audit_log','TRUNCATE');
   SELECT NOT rolcanlogin FROM pg_roles WHERE rolname='sms_app';"
```

期望七角色计数为 7 且属性检查为 true，accept 的 audit 权限为
`true|false|false|false`，旧角色为 NOLOGIN。再运行当前版本迁移一致性检查，并由隔离
API 使用职责角色执行 `/livez`、`/readyz`、只读查询和 mock UAT；未完成前禁止把恢复库
用于生产。

## 生产恢复

生产恢复属于重大变更，必须有安全负责人批准、确认回退点和停机公告。只启动 postgres/redis/redis-auth/redis-control，停止 api、worker-realtime、worker-bulk、worker-callback、outbox-dispatcher、beat 与 web；在全新目标库或已明确销毁的故障库执行与隔离恢复相同的哈希校验和流式 `pg_restore`。随后按顺序：

1. 运行 `migrate` 补齐当前 Alembic 与未来 13 个月分区。
2. 复核七角色非 owner/非超级用户/无 DDL、audit_log 不可修改删除，旧 sms_app 已停用。
3. 核对关键表计数、最新 raw_vendor_log/job_run 时间和备份时点后的数据缺口。
4. 先启动 api，确认 `/livez` 后等待 `/readyz`，再完成 `/metrics` 与登录检查；随后启动 realtime/bulk/callback worker、唯一 outbox-dispatcher、单实例 beat 与 web。
5. 对 RPO 窗口按厂商 raw/customId 和 unmatched 流程补对账；uncertain 禁止自动重发。

恢复成功后保留旧故障卷只读直到变更单关闭。失败则停止所有业务进程，回到已验证备份或原只读卷，不得反复覆盖唯一副本。

## 保留与销毁

备份文件、SHA-256 台账、恢复报告和访问记录按公司备份策略保存；关键日可提高频率。销毁须同时删除主/备对象存储副本和 KMS 授权，记录对象版本与审批人。严禁把 `.dump.enc`、`.sha256` 或任何解密中间物提交 Git。
