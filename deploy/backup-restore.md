# PostgreSQL 加密备份与恢复手册

## 适用范围与职责

本手册适用于主节点日常全量备份、发布前回退点和隔离恢复演练。仅 DBA 可使用 `sms_owner` 执行；七个运行角色均不得备份、恢复或创建数据库。数据库内手机号虽已加密，备份仍属于高敏资产，必须二次加密、0600 保存、限权传输并按审批周期销毁。

备份口令不是平台运行 secrets 清单的一部分，必须由备份/KMS 系统单独托管。Phase 0 固定从
持久路径 `/etc/sms-platform/backup-secrets/sms-backup-passphrase` 读取：父目录 `root:root 0700`、
文件 `root:root 0600`、普通文件且不得是链接。禁止使用重启后消失的 `/run` 路径，也禁止把
口令值放进环境变量、命令参数、`.env`、工单或 shell history。

## 生产自动备份、隔离恢复与证据账本

生产默认使用 `lifecycle_manager.py` 复用本手册的流式加密备份和隔离恢复实现：

- `sms-backup.timer` 每天 02:30 与 14:30 各生成一次 AES-256-CBC/PBKDF2 密文快照，并保留
  最多 15 分钟随机延迟；相邻计划窗口不超过 12 小时 15 分钟，为 RPO≤24h 留出备份耗时和
  一次失败重试余量。逐文件核对大小与 SHA-256；
- 生产 `sms-lifecycle-status.timer` 每小时运行 `backup-status`，核对最新备份 manifest、大小、
  SHA-256、真实备份时点和 RPO；它不执行恢复，也不把同机结果标成可恢复；
- full restore 只在具有独立 PostgreSQL/VMDK、禁止厂商生产发送出站并放置
  `/etc/sms-platform/preproduction-restore-host` marker 的一次性空白隔离恢复机手动执行。
  Phase 0 没有获批自动传输链，因此生产和预生产都不启用 `sms-restore-drill.timer`；
- 三类失败都向 journal 输出固定 `event=lifecycle_alert`、操作名和异常类型，不输出路径、
  命令错误、手机号、密钥或其他 PII；service 以 5 分钟间隔最多重试三次。

Phase 0 的生产快照隔离恢复只允许在从预生产资源池按需创建的**一次性、空白、隔离恢复机**
执行；共享应用预生产只承担候选发布与业务预验收，不得安装生产 recovery bundle、生产备份
口令或生产 generation ID，也不得承担生产快照恢复演练。恢复前必须确认三只 lifecycle timer
均已停止/禁用，并按 snapshot manifest 的两个非敏感 generation ID 从离机 escrow 取回
recovery-crypto bundle 与 backup passphrase；canonical 25-secret、口令、两个固定 ID 文件、marker
及受控 runtime generation 的精确安装/读回步骤见 [failover.md](failover.md)。恢复完成并归档证据
后按批准的 VMware 流程整体退役该主机，不通过手工挑删文件把它改回共享预生产。

生产完整性校验成功只形成 `scope=production_backup` 的备份/RPO 证据，不形成
`available=true`。隔离恢复主机对同一 snapshot ID/manifest digest 完成流式解密恢复、Alembic、
关键表/列、七角色权限、旧角色停用和只读业务查询后，才形成
`scope=preproduction_restore` 的恢复证据。两份证据必须在变更单中配对；不得根据 `current`
符号链接、“文件存在”或生产 `backup-status` 单独推断可恢复。

隔离恢复的 `max_restore_seconds` 是数据库恢复工程上限，不等同于从 `outage_start` 计算的业务
RTO。业务 RTO 必须同时包含发现、决策、围栏和旧/新平台最小验收，并按
[failover.md](failover.md) 单独记录。任一 service 失败立即产生告警。

账本和报告均以 0600 原子替换，不保存备份路径、口令、密文内容或业务数据。默认保留 35
天且至少保留两份；清理只接受固定 snapshot ID、只删除保留边界外且不是 `current` 的目录，
并在账本记录边界、删除数与保留数。安装与启用命令见 [README.md](README.md)。
每天两份意味着稳定窗口最多约 70 个 snapshot generation；上线前必须用预生产实测密文大小
投影 35 天占用，确保 Runtime VMDK 低于 70% 告警线。预计越线时先在线扩容，不得缩短 35 天
保留、删除未过期恢复点或把备份搬到 OS/Docker/PostgreSQL VMDK。

## 备份前检查

1. 记录 Git commit、Alembic version、PostgreSQL 16 小版本、执行人、目标保留期和变更单号。
2. 确认固定备份根 `/var/lib/sms-platform/runtime/backups` 是 Runtime VMDK 上的
   `root:root 0700` 非链接目录，且剩余空间大于数据库已用空间的 1.5 倍。
3. 确认口令文件存在、权限 0600，且不在仓库目录。
4. 对发布回退点暂停会产生 schema 变更的操作；日常在线备份不需要停止业务。
5. 生产通用 `sms-compose exec` 已失败关闭；不得为了读 Alembic、执行 `pg_dump`
   或查表而放开它。这些预检、导出和无 PII 证据由已安装的 lifecycle service
   在固定参数、锁和日志边界内执行。

## 流式加密全量备份

生产唯一备份入口是 root-owned `sms-backup.service`，定时由
`sms-backup.timer` 调用；首份备份或批准的手动窗口用：

```bash
sudo systemctl start sms-backup.service
sudo systemctl start sms-lifecycle-status.service
sudo systemctl show --property=Result --property=ExecMainStatus \
  sms-backup.service sms-lifecycle-status.service
```

两个 service 任一启动失败、`Result` 非 `success` 或 `ExecMainStatus` 非 `0` 即停止。
`lifecycle_manager.py backup` 在锁内把 `pg_dump` 标准输出直接送入
PBKDF2 + AES-256-CBC，不写明文 dump、临时 SQL 或未加密 tar；它原子生成
`0600` snapshot manifest/哈希/账本，并记录不含 PII 的计数、Alembic 与密码学代次。
不得用手工管道代替，也不得把密文哈希或 service 退出 0 单独解释为可恢复。

进程退出码 0 只表示该次 backup/status 命令在用户态走完了检查与返回。Power-Loss Durable
是另一条合同：Dump、Archive、Snapshot rename、`current` 切换和 Lifecycle
State 替换都必须先 `fsync` 文件与父目录，再越过对应提交屏障
（`staging → payload_durable → snapshot_published → current_switched →
ledger_committed`）。只有命令成功返回且这些屏障都完成后，重挂载才保证仍能找到
完整可验证 Snapshot 以及与 `current` 一致的账本。未完成屏障的中断必须把未登记
Snapshot 隔离到 `orphans/`（保留 24 小时）或回退 `current`，不得把 page cache 里
的成功当成掉电后仍可恢复。生产等价故障注入与 ReleaseStore 相同：以文件
`fsync`、父目录 `fsync` 和 `os.replace` 为提交屏障；测试在这些屏障上模拟进程
消失，不把 page cache 里的成功当成 ext4/XFS 掉电后仍可恢复。

## 密文与可恢复性校验

仅计算出哈希不代表可恢复。首份生产备份、重大迁移和恢复窗口都必须把密文快照送到独立
一次性空白隔离恢复机并恢复到隔离库。安装完整 canonical 25-secret、
backup passphrase、两个固定 ID 与 marker 后，先使用 [failover.md](failover.md) 记录的
`release start-recovery` 公开参数；包装器会在 lifecycle lock 内校验 canonical 清单、生成
不可变 runtime generation、绑定其 target，并在消费面关闭时只启动 PostgreSQL 和
三个 Redis 数据服务。禁止普通 `up`、手工调用密钥 prepare 或修改 `/run`。

这里的 runtime target 绑定只防止容器混用 runtime generation，不证明 canonical 材料的
escrow 来源。snapshot 中的两个 generation ID 只是非敏感标签，不是密钥/口令材料的摘要、
MAC 或密码学绑定；`crypto_generation_binding=matched_host_generation_ids` 也只表示 manifest
与主机 ID 字符串相等。上线 No-Go 要求离机 escrow 按 ID 原子发放不可变 bundle、两名真实
人员见证来源与整包 provision，且一次性恢复机所需的历史 data AES/HMAC、四个 audit keys
（按需含 alert pair）只能来自该 bundle。探针成功只能证明当次已安装材料能解密其覆盖样本，
不能证明 escrow provenance、所有历史行或未来仍可取得材料。

只有上述入口成功且四个数据服务健康后，才可使用官方隔离演练入口：

```bash
sudo /usr/bin/env \
  BACKUP_PASSPHRASE_FILE=/etc/sms-platform/backup-secrets/sms-backup-passphrase \
  DRILL_ENV=1 \
  /usr/bin/python3 /opt/sms-platform/deploy/scripts/restore_drill.py \
  /var/lib/sms-platform/runtime/backups/imported/<snapshot-id>/<database-file>.dump.enc \
  /var/lib/sms-platform/runtime/backups/imported/<snapshot-id>/manifest.json \
  --report /var/lib/sms-platform/runtime/backups/reports/<snapshot-id>.json \
  --max-restore-seconds 43200
```

尖括号只替换为已与生产 `backup-status` 证据逐项匹配的固定文件名；禁止
`current`、glob 或“最新文件”。脚本自行完成哈希校验、流式解密/恢复、限时清理
和原子报告；操作者不得展开为 `dropdb`/`createdb`/`pg_restore` 或通用
`sms-compose exec` 管道。脚本失败后将该快照标为不可用并保留无敏感错误类型；
不手工清理来伪造成功。

## 隔离恢复验收

不手工执行 SQL 验收。官方报告必须与生产侧同一 snapshot ID、
snapshot manifest SHA-256 与 database SHA-256 匹配，并显示：Alembic 从快照 head
迁移到当前 head；关键表/列与无 PII 计数；七角色安全属性；`audit_log`
只增不可修改/删除；旧 `sms_app` 为 NOLOGIN 且无授权；`sms_accept` 固定只读
业务查询通过；数据库恢复耗时不超过工程上限。

密码学探针在 manifest Alembic 核对后、任何 migrate/密钥表回填前首先以只读
事务执行，迁移后再执行一次。恢复报告固定为 schema v2，并携带
`crypto_probe_receipts.pre_migration`/`post_migration`；每份 receipt 只接受
`schema_version`、`status`、`counts`、`coverage` 四个顶层字段。它把 snapshot 中
`audit_context_signing_key` 的主体、API、realtime、bulk 共四个审计 key 与 recovery
bundle 精确比对，扫描 public schema 的全部 `*_key_version` 引用并要求每个引用代次
存在于 data AES/HMAC keyring。

`coverage` 必须精确等于 schema 中 17 个持久密文字段的闭集，不能多也不能少：
`app.callback_secret_enc`、`blacklist.phone_enc`、`callback_task.callback_secret_enc`、
`import_phone.phone_enc`、`raw_vendor_log.payload_enc`、`reply_event.content_enc`、
`reply_event.phone_enc`、`report_event.phone_enc`、
`sensitive_metadata_archive.value_enc`、`sms_batch.display_content_enc`、
`sms_batch.send_content_enc`、`sms_message.phone_enc`、`sms_reply.phone_enc`、
`sms_template.content_enc`、`sms_template.name_enc`、`unmatched_report.phone_enc`、
`vendor_test_recipient.phone_enc`。探针对每个非空字段的每个实际密钥版本抽取一条，
按该字段的真实认证上下文解密；phone 字段另复核 HMAC，raw payload 另复核明文
SHA-256。`counts.encrypted_rows` 是这 17 个字段的总行数，`coverage` 的每项固定为
`rows`/`key_versions_verified`。

该结果只证明闭集、全部被引用代次和逐字段逐版本样本，不得外推为 17 个字段的
所有历史行、历史告警密文/alert keypair 或 `audit_log` MAC 已验证。
`checks.historical_ciphertext_validation` 绑定 pre-migration receipt；pre/post 是两份
独立 receipt，迁移造成数据变化时状态可以不同。

首份 pre-T0 空库恢复除 `table_counts.sms_message=0` 外，还必须在两份 receipt 中看到
`counts.encrypted_columns=17`、`counts.encrypted_rows=0`、17 项 `coverage.rows` 全为 0，
且 pre/post 与绑定 pre 的汇总状态均为 `not_applicable_empty`；这表示全部 17 个密文字段
为空，只能作为初始化前门禁。首批最小 notice 后、3 天观察期内必须对新生产快照使用
新的一次性恢复机复验；报告必须为 `table_counts.sms_message>0`，两份 receipt 的
`status=performed` 且绑定检查均为 `performed`，否则不得扩大到更多应用、verify 或 market。

## 生产恢复

当前 Phase 0 只封口了“一次性隔离恢复机的快照验证”，没有封口把密文快照
恢复到 live `sms` 库并逐级恢复消费面的 dedicated production action。因此真实新平台
生产恢复在该入口实现、契约测试和隔离演练全部完成前为 **No-Go**；不得用
通用 `sms-compose exec`、手工 `pg_restore`、普通 `up`、raw Compose 或手改 `/run`/
release state 拼接生产恢复。故障时按 [failover.md](failover.md) 先冻结/围栏新平台，
再受控切回使用不同服务商的旧系统以恢复发送能力，新平台保持停服并另行计时。

后续 dedicated action 至少必须在同一 lifecycle lock/持久 recovery fence 内实现：
快照 manifest/database digest 绑定与流式恢复；恢复前/后密码学探针和 migrate；
不可变 restore receipt 及两名真实人员复核；gap fence 绑定；先 API 只读验收，
再 callback→发送 workers→单个 outbox-dispatcher→单实例 beat→web 的固定恢复顺序；
任一失败重新围堵并持久化 `failed/recovery_required`。实现前不得在文档中宣称该路径可执行。

## 保留与销毁

备份文件、SHA-256 台账、恢复报告和访问记录按公司备份策略保存；关键日可提高频率。销毁须同时删除主/备对象存储副本和 KMS 授权，记录对象版本与审批人。严禁把 `.dump.enc`、`.sha256` 或任何解密中间物提交 Git。
