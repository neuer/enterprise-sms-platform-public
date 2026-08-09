# 冷备同步、恢复演练与故障切换手册

## 目标、边界与值班责任

本手册用于 PostgreSQL 16 单主冷备。每日成功快照保证 **RPO≤24h**；从宣布切换到恢复受控业务的目标为 **RTO≤30min**。冷备节点平时只保存加密数据库、Git 归档、无密钥生产配置和校验清单，**不启动**任何平台容器。`sync_standby.py` **不传输 24 件运行 secrets**，也不会执行 Docker 启动命令。

真实备机、DNS、厂商白名单和生产流量切换需要值班负责人、DBA、安全与网络人员共同执行，属于 `[HANDOVER]`。任何步骤不满足时以保持停服为最安全状态，禁止为了赶 RTO 绕过凭据、出口 IP、哈希或单主检查。

## 一次性准备

1. 主、备节点安装 Docker Compose v2、Python 3.12、OpenSSL、rsync、SSH 和 PostgreSQL 16 客户端；创建无登录特权的 `smsdr` 同步账户，快照根目录权限 0700。
2. 密钥系统在主、备节点分别落生产 24 件 secrets。首次部署和每次轮换均由两名不同人员逐项核对文件名、版本、属主与 0600 权限并登记；不得通过 rsync、Git、`.env` 或工单复制值。详见 [secrets.md](secrets.md)。
3. 厂商工单书面确认主出口 IP、备出口 IP 属于同一账号白名单，QPS 和单次号码上限一致，并在两端执行 GetBalance。任一端返回 1010 时不得切换。详见 [vendor-egress.md](vendor-egress.md)。
4. 为 API 域名预设低 TTL；准备仅供演练的 `/etc/hosts` 映射。DNS 变更前后均保留解析结果、TTL、变更单和回退值。
5. 备机防火墙默认拒绝应用出站；只有切换审批完成后才开放厂商、LDAP、告警与回调所需目的地。

## 每日同步与 RPO 巡检

主节点的生产环境文件必须是 0600，且包含 `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`；疑似凭据的键只能是指向 `/run/secrets/` 的 `_FILE` 路径。备份口令由独立 KMS/备份系统写入仓库外 0600 文件，只通过 `BACKUP_PASSPHRASE_FILE` 传入文件路径。

手工验证一次后执行：

```bash
export BACKUP_PASSPHRASE_FILE=/run/backup-secrets/sms-backup-passphrase
export STANDBY_HOST=sms-backup01.internal
export STANDBY_USER=smsdr
export STANDBY_ROOT=/srv/sms-standby
export STANDBY_SSH_PORT=22
python3 deploy/scripts/sync_standby.py \
  --environment-file /etc/sms-platform/production.env \
  --output-dir /var/lib/sms/standby-sync \
  --database sms
```

脚本先检查 tracked 工作树干净，再流式执行 `pg_dump | openssl`，生成 Git commit、Alembic version、文件大小与 SHA-256 清单；本地和远端均先写 `.incoming`，校验成功后原子更新 `current`。归档来自 `git archive HEAD`，忽略目录和 secrets 不在归档内。失败不会移动 `current`，值班必须告警并修复，不能把旧快照误报为新备份。

### systemd（推荐）

`/etc/systemd/system/sms-standby-sync.service`：

```ini
[Unit]
Description=SMS platform encrypted cold-standby sync
After=docker.service network-online.target

[Service]
Type=oneshot
User=smsbackup
WorkingDirectory=/opt/sms-platform
Environment=BACKUP_PASSPHRASE_FILE=/run/backup-secrets/sms-backup-passphrase
EnvironmentFile=/etc/sms-platform/standby-sync.env
ExecStart=/usr/bin/python3 deploy/scripts/sync_standby.py --environment-file /etc/sms-platform/production.env --output-dir /var/lib/sms/standby-sync --database sms
UMask=0077
```

`/etc/systemd/system/sms-standby-sync.timer`：

```ini
[Unit]
Description=Daily SMS platform cold-standby sync

[Timer]
OnCalendar=*-*-* 02:30:00 Asia/Shanghai
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

启用后每天检查 `systemctl status sms-standby-sync.timer`、最近一次 service 退出码，以及远端 `current/manifest.json` 的时间和 SHA-256。快照年龄达到 20 小时先预警，超过 24 小时视为 RPO 违约并升级 crit。

### cron（仅在没有 systemd timer 时）

不得同时配置 systemd 与 cron。替代项如下，包装脚本只导出上述固定路径变量并把退出码送入内部告警：

```cron
30 2 * * * cd /opt/sms-platform && BACKUP_PASSPHRASE_FILE=/run/backup-secrets/sms-backup-passphrase STANDBY_HOST=sms-backup01.internal STANDBY_USER=smsdr STANDBY_ROOT=/srv/sms-standby python3 deploy/scripts/sync_standby.py --environment-file /etc/sms-platform/production.env --output-dir /var/lib/sms/standby-sync --database sms >/var/log/sms-standby-sync.log 2>&1
```

## 自动可恢复性与周期演练

`sms-backup.timer` 每日生成本地加密快照，`sms-restore-drill.timer` 每周随机恢复，
`sms-lifecycle-status.timer` 每小时验证 RPO/RTO 证据。恢复脚本只接受 `sms_drill_*`
数据库，先验证 manifest、大小与 SHA-256，再执行流式解密和 `pg_restore`；它不会产生
明文 dump，也不会启动 api、worker 或 beat。

```bash
export BACKUP_PASSPHRASE_FILE=/run/backup-secrets/sms-backup-passphrase
python3 deploy/scripts/restore_drill.py \
  /var/lib/sms/standby-sync/current/sms_<snapshot>.dump.enc \
  /var/lib/sms/standby-sync/current/manifest.json \
  --report /var/lib/sms/drill-reports/<snapshot>.json
```

报告必须显示：恢复耗时小于 1800 秒、Alembic 可迁移到当前版本、关键表和手机号密文四列
结构齐备、七个职责角色属性安全、旧 `sms_app` 为 NOLOGIN 且无授权、`audit_log`
不可修改/删除、`sms_accept` 只读业务查询通过，以及三个不含 PII 的表计数。脚本默认在
`finally` 删除演练库；仅隔离环境显式 `DRILL_ENV=1 --keep` 才可保留。故障切换只能选择
`lifecycle-state.json` 中 `integrity_verified=true`、`restore_verified=true` 且
`available=true` 的快照；`current` 本身不是可恢复证据。

## 故障切换（单主强制顺序）

开始计时并记录事件编号。每一步完成后由另一名执行人复核。

1. **隔离旧主。** 停止旧主的 web、api、worker-realtime、worker-bulk、worker-callback、outbox-dispatcher、beat；主机不可达时在负载均衡、网络和容器编排层同时隔离其入站、Redis/PostgreSQL 与厂商出站。没有旧主已冻结的证据就停止切换，严禁出现双 dispatcher、双 beat、双拉取 GetReport/GetReply 或双发送 worker。
2. **锁定恢复点。** 从生命周期账本选择 `available=true` 的快照，再核验其
   `SHA256SUMS`、manifest、Git commit、Alembic version、最后演练报告 SHA-256、备份年龄、
   恢复耗时与 `data_gap_seconds`。超过 24 小时明确记录数据缺口与业务批准；完整性或演练
   证据失败则换上一份仍在保留期且已验证的快照，禁止直接使用 `current`。
3. **恢复基础设施。** 解包同 commit 的 Git 归档，人工确认 24 件 secrets 版本，使用 [backup-restore.md](backup-restore.md) 的流式步骤恢复生产 `sms`。先只启动 `postgres`、`redis`、`redis-auth`、`redis-control`，执行 `migrate`，复核角色、audit 权限、表计数和 `/metrics` 所需事实源。
4. **启动无消费入口。** 启动 `api`，先验证 `/livez`，再等待 `/readyz`、`/metrics`、JWT 登录和只读列表；此时 DNS 仍指向维护页，禁止发送接口流量。
5. **逐队列启动。** 先启动 `worker-callback`，再启动 `worker-realtime`、`worker-bulk`；worker 就绪后只启动一个 `outbox-dispatcher`，观察 pending/dead/最老事件年龄回落。确认队列深度、vendor 令牌桶、余额和暂停开关正确；不得手工重投 submitted 或 uncertain chunk。
6. **恢复唯一调度。** 再次确认旧主 beat 和所有拉取进程已隔离，随后只在备机启动单实例 `beat`。观察 job_run 心跳至少两个最短任务周期，确认没有 `job_stalled`。
7. **开放流量。** 启动 `web`；先用演练 `/etc/hosts` 验证完整登录、受理和查询，再修改 DNS。确认解析已指向备机、旧地址无流量后移除维护页。
8. **核验厂商链路。** 从备出口 IP 调 GetBalance，确认未返回 1010；用受控最小批次验证发送、GetReport/GetReply、回调与告警 log-sink。未收到书面白名单确认不得发送。

## RPO 窗口对账

恢复点到旧主冻结之间的数据可能不在快照。先保存并加密厂商拉取原始响应到 `raw_vendor_log`，事务提交后再解析；对陌生 customId 落 `unmatched`，不得丢弃。以 raw 的 custom_ids 与平台 chunk 对照：

- submitted 按厂商报告推进，不做重复下发；
- uncertain 只由 reconcile 依据 customId 修复为 submitted 或 failed，**禁止自动重发**；
- 无厂商记录且从未提交的 pending 才能走事实源兜底投递；
- GetReport/GetReply 是拉走即消费，切换期间始终只允许一端轮询。

对账结果记录快照时点、缺口范围、raw/unmatched/uncertain 数量、人工处置和复核人，不记录手机号明文、密文或 HMAC 列表。

## 失败回退与回切

切换任一步失败时，立即关闭备机 DNS 入口，停止 beat、Outbox dispatcher 与全部 worker，保留 PostgreSQL 卷和日志只读。若旧主可证明一致且未继续写入，可恢复旧主 secrets/出口/DNS；否则回到上一份已通过恢复演练的快照。禁止在两个节点之间反复开放写流量。

故障修复后的回切按一次新的故障切换执行：先把当前备机提升后的生产事实做加密快照并恢复到原主隔离库，完成哈希、迁移、权限与计数验证；冻结当前主并记录最终 RPO 边界；按 `postgres → redis → api → worker-callback → worker-realtime/worker-bulk → outbox-dispatcher → beat → web` 顺序启动原主；最后切 DNS 和厂商出口。旧节点至少保留一个观察周期且保持隔离。

## `[HANDOVER]` 生产演练记录

无人值守门禁只验证本地真实加密备份、隔离恢复、清理和脚本化安全条件；以下必须在已报备的真实主备环境完成后填写，才构成生产 **RTO≤30min** 证据：

- 事件/变更单、主备资产、执行人与双人复核人：`[HANDOVER]`
- 主出口 IP、备出口 IP 与厂商书面确认编号：`[HANDOVER]`
- 最后成功快照 UTC、恢复点 UTC、实际 RPO：`[HANDOVER]`
- 旧主冻结证据、唯一 beat/唯一拉取证据：`[HANDOVER]`
- 开始、数据库恢复、API 可用、worker/beat 可用、DNS 生效时间：`[HANDOVER]`
- 实际 RTO、是否 `≤1800s`、超时原因与整改：`[HANDOVER]`
- raw_vendor_log/unmatched/uncertain 对账结果与回切结果：`[HANDOVER]`
