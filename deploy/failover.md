# Phase 0 备份恢复、旧系统切回与后续冷备手册

## 目标、边界与值班责任

本手册用于 PostgreSQL 16 单主恢复和首批应用受控切回。Phase 0 目标为每日成功备份支持
平台数据 **RPO≤24h**，短信发送能力业务 **RTO≤12h**。`outage_start` 取最早业务不可用
证据或受控关闭新平台入口时刻；发现、决策、冻结和在途分类全部计入。首批应用通过新平台
恢复，或在发送面完成围栏后切到旧系统并完成最小 notice 验收，任一路径可停止业务 RTO。
新平台恢复耗时另记为 `platform_recovery_elapsed`。数据库加密
备份保留 **35 天**。当前已接受冷备节点和备出口暂非首发硬门禁；因此 Phase 0 只能声称
“从备份恢复”，不得声称主备切换、跨机房容灾或高可用已经具备。
首发总入口与证据矩阵见
[production-phase0-baseline.md](../docs/runbooks/production-phase0-baseline.md)。

旧短信系统使用不同服务商长期并行，可在新平台故障时承接首批应用的发送能力。受控切回可
恢复业务发送能力并停止 business-RTO，但不恢复新平台数据库、审计、报告或在途状态，不能
抵扣 RPO，也不是新平台恢复证据。恢复期提交到旧系统的短信不得在新平台补录后自动重发。

后续建成冷备后，冷备节点平时只保存生命周期账本已判定 `available=true` 的加密数据库快照、
内部 Git 归档、无密钥生产配置和校验清单，**不启动**任何平台容器。Phase 0 不授权启用
`sync_standby.py` 或另起一套 `pg_dump`；冷备复制必须作为后续独立变更接入同一生命周期账本。

真实恢复目标、DNS、厂商白名单和生产流量切换需要值班负责人、DBA、安全与网络人员共同
执行，属于 `[HANDOVER]`。任何步骤不满足时以保持停服或切回旧系统为最安全状态，禁止
为了赶 RTO 绕过凭据、出口 IP、哈希或单主检查。

## Phase 0 首发边界与 No-Go

- 生产通过 VPN 访问；VPN、TLS 终结、精确入口网段和主节点固定厂商出口的真实连通证据
  属于 `[HANDOVER]`。配置文本、截图或文档契约测试不能替代网络侧读回。
- 生产从空库初始化；测试数据库、数据、API Key、26 件 canonical secrets（含 Redis ACL 密码）、备份
  口令和运行目录不得复制或复用。缺少逐项独立生成和权限复核记录即 No-Go。
- 每日加密备份必须保留 35 天。生产 bootstrap 后、初始化管理员/API Key/T0 前，必须生成首份
  生产加密快照并在从预生产资源池按需创建的一次性空白隔离恢复机完成 full restore；记录恢复
  点年龄和数据库恢复耗时。只有备份文件、哈希或成功退出码不构成恢复证据。
- 冷备与备出口暂非首发硬门禁，但必须记录为残余风险。没有冷备时，单主机或主出口故障
  只能停服恢复或切回旧系统，不得沿用下文冷备步骤宣称已完成故障切换。
- 企业微信与公司邮件必须各完成一次真实告警投递和接收人/替补确认；`log-sink`、Mock
  或测试函数通过不构成生产告警闭环。`lifecycle_manager.py`、存储预检和 systemd unit 只向
  journal 输出固定事件；必须由宿主外采集/告警链转发，且 VMware 另有整 VM 不可达告警。

## Phase 0 单主恢复准备

1. 在生产主机之外预留可承载隔离恢复的 VMware 资源配额；每次从预生产资源池创建
   一台一次性、空白、隔离恢复机，PostgreSQL 数据盘必须与生产 VM/VMDK 隔离。
   共享应用预生产不得承担生产快照恢复。该按需资源不等同于已建成冷备。记录
   CPU、内存、磁盘、网络、镜像 digest 和预计申请耗时，不写入地址或凭据。
2. 启用 `sms-backup.timer` 每天 02:30/14:30 各生成一次本地加密 PostgreSQL 备份，最多随机
   延迟 15 分钟，保留 35 天；该 12 小时级窗口为 RPO≤24h 留出备份耗时和失败重试余量。
   备份口令使用仓库外 root-owned `0600` 文件。当前没有 KMS，不得把文件边界描述为 KMS 托管。
3. 生产 timer 固定启用 partition maintenance、`sms-backup.timer` 和运行 `backup-status` 的
   `sms-lifecycle-status.timer`；生产
   PostgreSQL/VMDK 上禁止 full restore drill。把获批密文快照、manifest、SHA-256 和无 secret
   配置通过离线受控通道送到隔离恢复主机，在那里放置
   `/etc/sms-platform/preproduction-restore-host` marker 后手动执行官方 `restore_drill.py` 的显式
   snapshot/manifest 入口，
   核对 Alembic head、角色权限、关键表与无 PII 计数。记录恢复点、开始/结束时间和操作者复核。
4. 为首批 1–2 个低风险 notice 应用形成旧系统切回卡片：新平台 API Key 停用、调用方切换、
   旧服务商最小验证、在途批次处置和恢复新平台步骤。切回卡片至少在预生产演练一次。

## 后续冷备一次性准备（暂非首发硬门禁）

1. 主、备节点安装 Docker Compose v2、Python 3.12、OpenSSL、rsync、SSH 和 PostgreSQL 16 客户端；创建无登录特权的 `smsdr` 同步账户，快照根目录权限 0700。
2. 通过经审批的离线/密钥交付通道，按 snapshot manifest 的非敏感 generation ID 取回对应
   recovery-crypto bundle：数据 AES/HMAC keyring、四个审计 keyring，以及需要读取历史告警配置
   时的 alert keypair；另按独立 ID 取回 backup passphrase。厂商、DB/Redis 密码、JWT、LDAP、
   metrics 和 Redis TLS 使用恢复时**当前获批**的运行 generation，禁止为恢复复活已吊销的整套
   旧 25-secret generation；旧 JWT 会话视为失效，外部凭据重新验收。执行人可为唯一技术管理员；
   第二名具名的业务负责人或变更审批人以独立身份复核 ID、文件名、属主与 `0600`
   权限，不要求其持有平台管理员账号，也不得共享凭据。两个 ID 必须对应两名真实
   人员；无第二人可复核即恢复 No-Go，禁止同人使用两个 ID。复核不读取值、长度、
   摘要或哈希；不得用 rsync、Git、`.env`、聊天
   或工单传值。恢复稳定后的密钥轮换是另一项变更。详见 [secrets.md](secrets.md)。
3. 厂商工单书面确认主出口 IP；备出口建成后再确认其与主出口属于同一账号白名单，QPS 和单次号码上限一致，并在两端执行 GetBalance。任一切换目标返回 1010 时不得切换。详见 [vendor-egress.md](vendor-egress.md)。
4. 为 API 域名预设低 TTL；准备仅供演练的 `/etc/hosts` 映射。DNS 变更前后均保留解析结果、TTL、变更单和回退值。
5. 备机防火墙默认拒绝应用出站；只有切换审批完成后才开放厂商、LDAP、告警与回调所需目的地。

## 后续冷备同步的实现边界（当前未授权）

Phase 0 的唯一生产备份链是 `sms-backup.timer` 与生命周期账本。当前不得直接启用仓库中的
`sync_standby.py`，不得临时拼一套 cron、另一只 systemd timer 或第二次 `pg_dump`，否则会产生
两个互相矛盾的“最新快照”和未经演练的恢复点。冷备同步上线前必须作为独立代码/变更完成：

1. 只选择生命周期账本中 `integrity_verified=true`、`restore_verified=true`、
   `available=true` 的既有密文快照；不得直接选择名称为 `current` 的目录或刚生成但未演练的文件。
2. 只复制该快照、manifest、`SHA256SUMS`、恢复报告、精确内部 Git 归档和无 secret 配置；禁止
   再次导出数据库，禁止传输 25 件 secrets、备份口令、运行态 Redis AOF 或任何明文中间物。
3. 源和目标都必须位于受控备份/冷备存储，不得占用 OS、Docker 或 PostgreSQL VMDK；目标先写
   不可见临时代次，逐文件核对大小和 SHA-256 后再原子发布 generation。
4. 复制结果回写同一 lifecycle state，保留快照 ID、备份时点、传输时点、目标代次、结果和
   无敏感错误类型。失败不能推进 `available` 指针，必须由宿主外告警链送达企微和公司邮件。
5. 只允许一只经评审的每日 systemd timer；不提供 cron 旁路。快照年龄 20 小时预警，超过
   24 小时按 RPO 违约升级 critical，远端仍执行 35 天保留和恢复演练。

## 生产备份状态与隔离恢复证据

生产 `sms-backup.timer` 每日至少生成两份本地加密快照；生产
`sms-lifecycle-status.timer` 每小时运行 `backup-status`，只验证最新快照完整性和 RPO，不把同机
数据库恢复或文件存在标成“可恢复”。生产不得启用 `sms-restore-drill.timer`。

生产快照 full restore 只在从预生产资源池按需创建的**一次性、空白、隔离
恢复机**执行。该主机必须有独立 PostgreSQL/VMDK、无生产厂商发送出站、无原有
release/lifecycle/runtime 状态，证据归档后整 VM 退役。共享应用预生产只承担应用
预发布，不安装生产 recovery bundle、backup passphrase、generation ID 或 marker，
也不承担生产快照恢复演练。

快照传输自动化尚未建设，Phase 0 不启用 restore timer。必须把与生产
`backup-status` 匹配的 snapshot/database 密文、manifest 和两个 SHA-256 以显式固定
路径传入；禁止复制/手改生产 `lifecycle-state.json`、使用 `current`/glob/“最新文件”
或让隔离机自行猜选候选。

离线通道必须把一个 snapshot 作为完整目录交付：目录名等于 manifest 的固定 snapshot ID，
目录为 root:root `0700` 普通非链接目录；目录内**精确五个** root:root `0600` 普通非链接文件，
即 `manifest.json`、`SHA256SUMS`，以及 manifest `files` 中 `database`、
`repository_archive`、`environment` 三个互不重名的 payload，不得有额外文件。manifest 必须
声明 `database=sms`、`secrets_included=false`、40 位小写十六进制 Git commit、UTC 且不在
未来的 `created_at`，并记录两个 generation ID；三份 payload 的实际 size/SHA-256 必须逐字
匹配 manifest，`SHA256SUMS` 必须无重复且精确覆盖三份 payload 与 manifest。官方入口会对
全部三份 payload 做全文件哈希，而不只检查 database；任一不符即停止，不得补写清单绕过。

恢复机上首先记录 unit 启用状态，然后停止平台及所有维护/报告 timer：

```bash
sudo systemctl show --property=Id --property=UnitFileState \
  sms-platform.service sms-partition-maintenance.timer sms-backup.timer \
  sms-restore-drill.timer sms-lifecycle-status.timer security-report-collector.timer
sudo systemctl disable --now \
  sms-platform.service sms-partition-maintenance.timer sms-backup.timer \
  sms-restore-drill.timer sms-lifecycle-status.timer security-report-collector.timer
! sudo systemctl is-active --quiet sms-platform.service
! sudo systemctl is-active --quiet sms-backup.timer
! sudo systemctl is-active --quiet sms-restore-drill.timer
! sudo systemctl is-active --quiet sms-lifecycle-status.timer
! sudo systemctl is-active --quiet sms-partition-maintenance.timer
! sudo systemctl is-active --quiet security-report-collector.timer
```

按 snapshot manifest 的 `recovery_crypto_generation_id` 从离机 escrow 取回并放入
`/secure/staging/canonical/` 的固定材料是 `data_aes_key`、`data_hmac_key`、
`audit_context_key`、`audit_system_api_context_key`、
`audit_system_realtime_context_key`、`audit_system_bulk_context_key`；只在确需读取历史
告警配置时，同一 bundle 还必须提供 `alert_credential_public_key` 与
`alert_credential_private_key`。其余材料（包括不需读历史告警时的 alert pair）使用恢复
时**当前获批**的运行凭据。最终必须一次性安装 [secrets.md](secrets.md) 的完整
26 件 canonical 清单，不得只换 ID、只安装六/八件恢复密钥或复活已吊销的整套旧凭据：

generation ID 只是非敏感标签，不是 bundle 内容的摘要、MAC 或密码学绑定；代码与报告只能
比较 manifest/主机 ID 字符串，无法证明 escrow provenance。Phase 0 上线 No-Go 要求 escrow
以该 ID 原子发放不可变整包、两名真实人员见证来源和整包 provision，且一次性恢复机的上述
六项（按需八项）历史密码学材料只能来自该 bundle。后续实际 probe 成功只能证明当次已安装
材料能读取闭集样本，不能证明未抽样历史行或未来仍可从 escrow 取得材料。

```bash
sudo install -d -o root -g root -m 0700 /opt/sms-platform/deploy/secrets
for secret_name in \
  vendor_secret_name vendor_secret_key data_aes_key data_hmac_key \
  api_key_pepper_key \
  audit_context_key audit_system_api_context_key \
  audit_system_realtime_context_key audit_system_bulk_context_key \
  alert_credential_public_key alert_credential_private_key jwt_secret \
  ldap_bind_password metrics_scrape_token db_owner_password \
  db_auth_password db_accept_password db_send_password db_callback_password \
  db_export_password db_scheduler_password db_metrics_password \
  redis_broker_password redis_auth_password redis_control_password \
  redis_tls_server_key; do
  sudo install -o root -g root -m 0600 \
    "/secure/staging/canonical/${secret_name}" \
    "/opt/sms-platform/deploy/secrets/${secret_name}"
done

sudo install -d -o root -g root -m 0700 /etc/sms-platform/backup-secrets
sudo install -o root -g root -m 0600 \
  /secure/staging/sms-backup-passphrase \
  /etc/sms-platform/backup-secrets/sms-backup-passphrase
sudo install -o root -g root -m 0600 \
  /secure/staging/recovery-crypto-generation-id \
  /etc/sms-platform/recovery-crypto-generation-id
sudo install -o root -g root -m 0600 \
  /secure/staging/backup-passphrase-generation-id \
  /etc/sms-platform/backup-secrets/generation-id
for generation_file in \
  /etc/sms-platform/recovery-crypto-generation-id \
  /etc/sms-platform/backup-secrets/generation-id; do
  sudo test -f "$generation_file"
  sudo test ! -L "$generation_file"
  test "$(sudo stat -c '%U:%G:%a' "$generation_file")" = 'root:root:600'
done
sudo cmp -s /secure/staging/recovery-crypto-generation-id \
  /etc/sms-platform/recovery-crypto-generation-id
sudo cmp -s /secure/staging/backup-passphrase-generation-id \
  /etc/sms-platform/backup-secrets/generation-id

umask 077
printf '%s\n' 'preproduction-restore-host-v1' \
  > /secure/staging/preproduction-restore-host
sudo install -o root -g root -m 0600 \
  /secure/staging/preproduction-restore-host \
  /etc/sms-platform/preproduction-restore-host
sudo test -f /etc/sms-platform/preproduction-restore-host
sudo test ! -L /etc/sms-platform/preproduction-restore-host
test "$(sudo stat -c '%U:%G:%a' /etc/sms-platform/preproduction-restore-host)" \
  = 'root:root:600'
printf '%s\n' 'preproduction-restore-host-v1' \
  | sudo cmp -s - /etc/sms-platform/preproduction-restore-host
```

上述 canonical、passphrase、两个 ID 和 marker 必须全是 root:root、普通非链接文件；
canonical 目录精确 `0700`、其他目录按手册 `0700`，文件精确 `0600`。marker
内容精确为 `preproduction-restore-host-v1\n`。两名真实人员只复核来源批准、文件名、
属主、mode、普通/非链接类型和 ID 一致性，不读取 secret 值、长度、摘要或哈希。
任一读回失败即停止。

只能通过包装器公开参数启动隔离恢复围栏；`--runtime-secrets-target` 是包装器内部
绑定，操作者不得传入。发布 manifest 必须是已批准的 production no-delta 恢复
manifest，并与内部 Git/Registry 中的 commit、四镜像 ID 和迁移 head 一致：

```bash
sudo /usr/local/sbin/sms-compose release start-recovery \
  --manifest /secure/staging/recovery-release/manifest.json \
  --snapshot-manifest /var/lib/sms-platform/runtime/backups/imported/<snapshot-id>/manifest.json \
  --snapshot-manifest-sha256 <64-lowercase-hex> \
  --confirm-recovered-host

sudo /usr/bin/env \
  BACKUP_PASSPHRASE_FILE=/etc/sms-platform/backup-secrets/sms-backup-passphrase \
  DRILL_ENV=1 \
  /usr/bin/python3 /opt/sms-platform/deploy/scripts/restore_drill.py \
  /var/lib/sms-platform/runtime/backups/imported/<snapshot-id>/<database-file>.dump.enc \
  /var/lib/sms-platform/runtime/backups/imported/<snapshot-id>/manifest.json \
  --report /var/lib/sms-platform/runtime/backups/reports/<snapshot-id>.json \
  --max-restore-seconds 43200
```

`start-recovery` 会只启动 PostgreSQL 与三个 Redis，并确认 api/web/workers/outbox/beat
与厂商出站仍在围栏中；禁止普通 `up`、手工密钥 prepare、手改 `/run` 或通用
`sms-compose exec`。隔离演练脚本默认在 `finally` 清理临时库；报告归档后按批准
的 VMware 流程整 VM 退役，不手工挑删证据/密钥来把它改回共享预生产。

密码学探针在 manifest Alembic 核对后、migrate/密钥表回填前先执行，迁移后再
执行一次。schema v2 报告必须包含
`crypto_probe_receipts.pre_migration`/`post_migration` 两份固定格式 receipt；四个审计
key 与 recovery bundle 精确比对，全部 public `*_key_version` 引用必须存在于 data
AES/HMAC keyring。两阶段均校验 [backup-restore.md](backup-restore.md) 列出的 17 个
持久密文字段精确闭集，并对每个非空字段的每个实际密钥版本抽一条、按真实认证
上下文解密；phone 另验 HMAC，raw payload 另验明文 SHA-256。该证据不得外推为
闭集中所有历史行、历史告警密文/alert pair 或 `audit_log` MAC 已验证。

`checks.historical_ciphertext_validation` 绑定 pre-migration receipt，pre/post 是两份
独立证据且可能不同。首份 pre-T0 空库报告除 `table_counts.sms_message=0` 外，两份
receipt 还必须各自满足 `counts.encrypted_columns=17`、`counts.encrypted_rows=0`、
17 项 `coverage.rows=0` 和 `status=not_applicable_empty`；绑定 pre 的汇总检查也必须为
`not_applicable_empty`，仅作初始化前门禁。首批最小 notice 后、3 天观察期内必须以
新生产快照在新的一次性恢复机复验；报告必须为 `table_counts.sms_message>0`，两份
receipt 与绑定检查均为 `performed`，否则不得扩大到更多应用、verify 或 market。
报告的数据库恢复工程耗时不是从 `outage_start` 计算的 business-RTO。

## Phase 0 事故处置与旧系统受控切回

1. **开始计时并冻结请求入口。** 以最早不可用证据或受控关闭新入口时刻记录
   `outage_start`；冻结首批应用的新请求和自动重试，停用对应 API Key/入口，保持新旧路由互斥。
   停止 backup、restore、partition 和 security collector timers，避免事故中自动动作改变证据。
2. **先围栏发送面，再切旧系统。** 停止 web/api 的新受理、发送 worker、Outbox dispatcher、
   beat 和厂商 Send 出站；在 VMware、网络和厂商出站三层冻结故障主机，取得稳定状态水位并
   分类 queued/submitting/submitted/uncertain。submitted/uncertain 禁止重发，只有厂商和平台
   证据确认未受理的请求才可双人批准补发。围栏读回后才把首批应用切到旧系统并做最小 notice
   验收；新平台仍需的 GetReport/GetReply/对账只能以唯一、受控且不产生 Send 的路径继续。
   没有旧主冻结证据时，禁止在恢复目标启动任何消费者、dispatcher、beat 或轮询器。
3. **在围栏后恢复业务发送。** 完成第 2 步的冻结和稳定水位读回后，才把互斥
   路由切到使用不同服务商的旧系统，完成最小 notice 验收并记录 business-RTO
   终点。旧系统只处理旧服务商的新发送和报告；新平台仍保持消费/发送面关闭。
4. **锁定恢复点并保存证据。** 选择年龄不超过 24 小时且最近隔离恢复成功的备份，
   验证 snapshot/manifest/database SHA-256、Git commit、Alembic version 和恢复报告；
   保留故障卷只读。无合格恢复点或 RPO>24h 必须明确记录实际数据缺口。
5. **建立数据缺口证据，但不启动新平台。** 完成下文“RPO 数据缺口围栏”，
   用于将来 dedicated recovery action 的 gap-fence 批准。旧系统使用不同服务商，
   不能替新厂商提供 customId/账单证据；未分类的 submitted/uncertain 仍禁止重发。
6. **新平台 live 恢复当前为 No-Go。** 现有封口入口只支持一次性隔离恢复验证；
   将快照恢复到 live `sms` 库并按 API→callback→发送 workers→outbox→beat→web 恢复的
   dedicated action 尚未实现/验收。禁止通用 `sms-compose exec`、手工 `pg_restore`、
   普通 `up`、raw Compose、手改 `/run`/release state 或 `bootstrap` 拼接恢复。在 dedicated action
   封口前，新平台保持停服并记录 `platform_recovery_elapsed`。
7. **分别记录时间。** Phase 0 当前的 business-RTO 从 `outage_start` 到首批应用通过
   旧系统完成最小
   notice 验收，应≤43200s；新平台恢复从恢复动作开始到新平台受控业务完成，另记
   `platform_recovery_elapsed`。两者均不得抵扣平台数据 RPO。

## 后续冷备故障切换（建成后适用）

开始计时并记录事件编号。每一步完成后由另一名执行人复核。

1. **隔离旧主。** 停止旧主的 web、api、worker-realtime、worker-report、worker-bulk、worker-callback、outbox-dispatcher、beat；主机不可达时在负载均衡、网络和容器编排层同时隔离其入站、Redis/PostgreSQL 与厂商出站。没有旧主已冻结的证据就停止切换，严禁出现双 dispatcher、双 beat、双拉取 GetReport/GetReply 或双发送 worker。
2. **锁定恢复点。** 从生命周期账本选择 `available=true` 的快照，再核验其
   `SHA256SUMS`、manifest、Git commit、Alembic version、最后演练报告 SHA-256、备份年龄、
   恢复耗时与 `data_gap_seconds`。超过 24 小时明确记录数据缺口与业务批准；完整性或演练
   证据失败则换上一份仍在保留期且已验证的快照，禁止直接使用 `current`。
3. **恢复基础设施。** 解包内部只读 Git 镜像的同 commit 归档，按上述双人见证流程确认
   manifest 所记两个 generation ID 对应的 recovery-crypto 与 backup-passphrase escrow bundle，
   并另行交付当前获批的运行凭据；通过受控包装器
   和 lifecycle lock 启动 `postgres`、`redis`、`redis-auth`、
   `redis-control`，再按 [backup-restore.md](backup-restore.md) 流式恢复生产 `sms`。执行 `migrate`，
   复核角色、audit 权限、表计数和 `/metrics` 所需事实源；所有消费进程继续停止。
4. **建立数据缺口围栏。** 按下节冻结上游并完成恢复点到旧主冻结时点的请求/厂商证据分类。
   无围栏不得启动发送、拉取、dispatcher 或 beat。
5. **启动无消费入口。** 通过受控包装器启动 `api`，先验证 `/livez`，再等待 `/readyz`、
   `/metrics`、JWT 登录和只读列表；此时 DNS 仍指向维护页，禁止发送接口流量。
6. **逐队列启动。** 先启动 `worker-callback`，再启动 `worker-report`、`worker-realtime`、`worker-bulk`；worker
   就绪后只启动一个 `outbox-dispatcher`，观察 pending/dead/最老事件年龄回落。确认队列深度、
   vendor 令牌桶、余额和暂停开关正确；不得手工重投 submitted 或 uncertain chunk。
7. **恢复唯一调度。** 再次确认旧主 beat 和所有拉取进程已隔离，随后只在备机启动单实例
   `beat`。`beatdata` 是可重建调度元数据，不属于冷备恢复载荷；Docker VMDK 或 named volume
   丢失时从空调度库启动，不恢复或手工修改 `celerybeat-schedule` shelve。任务是否已经执行以
   PostgreSQL `job_run` 为唯一事实；空调度库的相对调度从本次启动重新计算，单个任务到新的
   调度到期点前至多会再经历一个完整 interval。到期后的实际入队、开始与完成没有该上限，
   仍受 beat tick、broker、队列和 worker 健康状态影响。观察 `job_run` 心跳至少两个最短任务
   周期，确认没有 `job_stalled`。
   `job_run` 看不到已发布但尚未开始的消息，不能单独证明补跑安全。若恢复门禁已经全部通过且
   `housekeeping` 确认逾期，还必须排除同名任务在 outbox、broker 队列及 worker
   pending/active/reserved 中在途，并接受仍可能重复执行的剩余风险，才允许管理员通过既有
   记审计入口 `POST /api/v1/web/admin/jobs/housekeeping/trigger` 触发一次；无法排除时等待受控调度，
   不得补跑。禁止直接改写 shelve、执行 `celery call` 或绕过该入口向 broker 发布任务。
8. **开放流量。** 启动 `web`；先用演练 `/etc/hosts` 验证完整登录、受理和查询，再修改 DNS。
   确认解析已指向备机、旧地址无流量后移除维护页。
9. **核验厂商链路。** 从备出口 IP 调 GetBalance，确认未返回 1010；用受控最小批次验证发送、
   GetReport/GetReply、回调与企业微信+公司邮件真实告警。未收到书面白名单确认不得发送。

## RPO 数据缺口围栏与窗口对账

恢复点到旧主冻结之间的 API 受理、幂等记录、批次、厂商提交和已拉取报告都可能不在快照。
`biz_id` 只是关联证据；对应数据库幂等行已经随恢复点之后的数据丢失时，它本身不能阻止重复
发送。恢复前必须建立以下 gap fence：

1. 冻结首批应用的新平台 API Key 和上游自动重试，把路由固定到维护页或旧系统；同一请求不得
   同时投向新旧平台。导出每个上游从 `(restore_point, freeze_time]` 的耐久请求台账，至少含应用、
   稳定 `biz_id`、受理时点和路由结果，不导出手机号、正文或任何密钥。
2. 向**新平台所用厂商**收集该窗口的 customId、受理/发送状态、账单及支持工单证据，并结合
   故障卷只读证据。旧系统使用不同服务商，其报告、账单和 GetReport/GetReply 不能证明新厂商
   是否已发送。
3. 对每条缺口请求逐一分类：厂商确认已受理/发送的永不重发；厂商明确确认未受理/未发送的，
   只能经业务和运维双人批准建立一笔新的受控事务；仍未知的禁止自动重发，保持人工决策。
   仅凭平台快照中“查不到”或重新使用原 `biz_id` 不得判定未发送。
4. 归档请求数、三类数量、证据引用、批准人与处置结果，不记录手机号、密文/HMAC 列表或短信
   内容。未完成 gap fence 时维持停服或旧系统路由，作为恢复 No-Go。

围栏完成后，先保存并加密厂商拉取原始响应到 `raw_vendor_log`，事务提交后再解析；对陌生
customId 落 `unmatched`，不得丢弃。以 raw 的 custom_ids 与平台 chunk 对照：

- submitted 按厂商报告推进，不做重复下发；stale invoking 恢复不得把已 submitted 的 chunk 降为 uncertain；
- `attempt=invoking` 且无法证明未受理时标 `inconsistent`，禁止自动重发或换供应商；
- uncertain 只由 reconcile 依据 customId 修复为 submitted 或 failed，**禁止自动重发**；
- 无厂商记录且从未提交的 pending 才能走事实源兜底投递；
- GetReport/GetReply 是拉走即消费：对新平台服务商/凭据，切换期间始终只允许当前获批恢复
  节点的一个轮询器；旧系统继续只轮询其旧服务商。两者可并行但不得交叉凭据、报告或数据域。

对账结果记录快照时点、缺口范围、raw/unmatched/uncertain 数量、人工处置和复核人，不记录手机号明文、密文或 HMAC 列表。

## 多供应商路由与灾难恢复

当前生产只注册智慧信息。路由账本已能记录 `selected_vendor` 与每次 attempt。灾难恢复时：

- Provider 超时/断连后的 `uncertain` **禁止**自动切换到任何第二供应商；
- 只有调用前确定性不可用，或协议明确拒绝且 `safe_to_failover=true` 才允许选另一供应商；
- 人工从 unknown 换供应商必须先完成 conservative terminal / 双人处置，并创建新批次；
- 第二生产供应商上线前，不得把平台暂停键解释为“已具备多账户高可用”。

## 失败回退与回切

切换任一步失败时，立即关闭备机 DNS 入口，停止 beat、Outbox dispatcher 与全部 worker，保留 PostgreSQL 卷和日志只读。若旧主可证明一致且未继续写入，可恢复旧主 secrets/出口/DNS；否则回到上一份已通过恢复演练的快照。禁止在两个节点之间反复开放写流量。

故障修复后的回切按一次新的故障切换执行：先把当前备机提升后的生产事实做加密快照并恢复到
原主隔离库，完成哈希、迁移、权限与计数验证；冻结当前主并记录最终 RPO 边界、重新执行 gap
fence；所有阶段通过受控包装器和 lifecycle lock，按
`postgres → redis → api → worker-callback → worker-report/worker-realtime/worker-bulk → outbox-dispatcher → beat → web`
顺序启动原主；最后切 DNS 和厂商出口。禁止 raw Compose 或人工 Docker 顺序。旧节点至少保留
一个观察周期且保持隔离。

## `[HANDOVER]` 生产演练记录

无人值守门禁只验证本地真实加密备份、隔离恢复、清理和脚本化安全条件；以下必须在真实
生产业务回退/恢复演练后填写，才构成短信发送能力 **business-RTO≤12h** 证据。若终点是旧系统
最小 notice 验收，新平台恢复继续单独完成和计时，但不是停止 business-RTO 时钟的前置条件。
只有在冷备实际建成并完成同样记录后，才能额外称为主备切换证据：

- 事件/变更单、主备资产、执行人与双人复核人：`[HANDOVER]`
- 主出口 IP 与厂商书面确认编号；备出口若已建成则补充对应编号：`[HANDOVER]`
- 最后成功快照 UTC、恢复点 UTC、实际 RPO：`[HANDOVER]`
- 旧主冻结证据、唯一 beat/唯一拉取证据：`[HANDOVER]`
- `outage_start`、冻结/围栏读回、旧或新平台最小 notice 验收时间：`[HANDOVER]`
- 实际 business-RTO、是否 `≤43200s`、超时原因与整改：`[HANDOVER]`
- 数据库恢复、API 可用、worker/beat 可用、DNS 生效时间及独立
  `platform_recovery_elapsed`：`[HANDOVER]`
- raw_vendor_log/unmatched/uncertain 对账结果与回切结果：`[HANDOVER]`
- gap fence 的窗口、上游请求数、厂商已发送/明确未发送/未知分类和双人批准：`[HANDOVER]`
- 旧系统切回开始/完成、发送恢复与新平台恢复的独立时间点：`[HANDOVER]`
- 企业微信与公司邮件告警的事件 ID、接收人及升级结果（不得记录 webhook/token）：`[HANDOVER]`
