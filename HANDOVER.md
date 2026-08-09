# 企业短信管理平台生产移交

> 本文件是 2026-07-15 的历史生产候选移交记录。当前日常流程以
> `MAINTENANCE.md` 为准，当前阻塞以 `PROGRESS.md` 为准。

2026-07-15 全库安全与前后端契约对齐、统一发布控制及远端 Mock 演练已合并并推送 `main`。仓库候选基线 `<redacted-commit>` 的 GitHub Actions run `<redacted-run>` 与 hosted Release Gate run `<redacted-run>` 均退出 0；已归档的本地 Trivy 0.70.0 发布门禁扫描 API、Web、PostgreSQL、Redis 四个最终镜像，均为 0 HIGH / 0 CRITICAL，并退出 0。P0 合并后的最终不可变证据归档到生产变更单与 release manifest，不再通过额外文档提交回填 SHA/run ID。本文件中的外部 TLS、真实 AD、生产 24 件 secrets、24 小时压测、主备 RTO 与真人 UAT 必须由生产责任人继续完成。

> M4.1 前后端页面对齐补丁已在全新 Mock 卷重新执行 G2，并完成四角色、PRD 18 页面形态和 390px 真实浏览器验收。生产候选必须包含 M4.1 最终交付提交；旧 `m4` 镜像不含该补丁。

## 1. 需人工完成清单

- [x] 代码基线 `<redacted-commit>` 已执行 `bash scripts/verify_release.sh`：API 0、Web 0、PostgreSQL 0、Redis 0；当前仓库候选 `<redacted-commit>` 的 CI run `<redacted-run>` 与 hosted Release Gate run `<redacted-run>` 全部成功。v1.6.14 四镜像/数据持久化候选 `<redacted-commit>` 的证据继续有效。生产仍须把四镜像推送到受控仓库并归档扫描报告、扫描器 digest 与最终镜像 digest（RepoDigest）；P0 最终 SHA 与 run ID 按本节开头的证据边界处理。
- [ ] 配置外部 TLS 终结器的证书、HTTPS 重定向与 HSTS（`max-age>=31536000; includeSubDomains`），按部署手册归档 curl 结果；内部 HTTP Nginx 不设置 HSTS。真实浏览器确认 CSP/Permissions-Policy 生效且控制台无 CSP violation。
- [ ] 数据库迁移后先执行 `sms-compose init-admin --show-temporary-password` 初始化本地管理员并完成首次改密；再在系统配置页保存、测试并启用 AD。在 AD 创建 admin、approver、operator、viewer 四类安全组，按审批结果配置角色映射；生产设置 `DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`，以四种角色完成真实 LDAP 登录。禁止运行 `seed-dev`。
- [ ] 由受控密钥系统分别向主、备节点落盘 24 件生产运行 secret：厂商两项、AES/HMAC、
  主体与自治事件两个审计 HMAC、企微 X25519 公私钥对、JWT、LDAP、metrics、`db_owner_password`、七个 `db_<role>_password` 和三个 Redis ACL
  密码；目录 0700、文件 0600，DB owner secret 只挂载 postgres/db-role-provision/migrate。
  按 [deploy/secrets.md](deploy/secrets.md) 双人复核，不复制 secret 值到 `.env`、工单、
  日志或聊天。
- [ ] 向厂商同单报备主、备出口 IP，书面确认真实 QPS、单次号码上限及生效时间；分别验证 GetBalance。将确认值写入 `sys_config.vendor_qps`、`reserved_realtime_qps` 与 `vendor_batch_size`，重启 beat 使调度配置生效。
- [ ] 在 `sys_config` 配置经审批的企业微信 webhook、SMTP relay/发件人与收件人；触发无 PII 测试告警并确认 `alert_log`、企微和邮件均收到。未配置前系统只使用 log-sink。
- [ ] 按 [deploy/failover.md](deploy/failover.md) 完成首次冷备同步、隔离恢复和真实 DNS/出口切换；记录 RPO，并以计时证据验证 RTO ≤30min。切换前必须证明旧主已冻结，禁止双 beat、双报告轮询或双发送 worker。
- [ ] 在隔离预生产按 [docs/PERFORMANCE.md](docs/PERFORMANCE.md) 执行固定 24 小时、100000 条 Locust 压测，归档 commit、镜像 digest、分布、P95/P99、失败率、资源峰值和排空时间。
- [ ] 按 [docs/UAT.md](docs/UAT.md) 完成真人 28 例并签字；01–04 必须在真实 AD、
  `AUTH_MOCK=0` 环境复验。自动化 20 项历史证据保存在受限归档，不随公开快照发布。
- [x] 已在隔离主机完成一次**远端 Mock 发布演练**：精确修复提交 `<redacted-commit>` 覆盖 Web-only、API-only、数据镜像验证、配置失败不变、健康失败补偿与 TERM/resume，最终退出 0；恢复后默认 10 个容器 ID 与 4 个卷在最终演练前后逐项一致，公网边界和管理员登录/退出浏览器冒烟通过。首次执行误清理默认 Mock 卷、测试环境重置及整改证据保存在受限归档，不随公开快照发布；不得描述为原测试数据未变化。`release_control_smoke` 只证明控制面，不代表发布就绪，也不替代正式 Trivy `release` 证据。
- [ ] 为生产统一四镜像发布建立**生产变更单**：绑定 release_id、commit、四个 RepoDigest/image ID、changed subset、迁移兼容性、维护窗口、Trivy/数据/备份恢复证据、执行/复核/回退人和终态；不得写 secret 或手机号。
- [ ] 变更关闭前保留发布包、旧镜像和事件记录，包括 `state.json`、`events.jsonl`、`residual_changes` 与上一成功回退点；发布工具不自动 prune。`recovery_required` 时禁止自动续跑、清理或猜测迁移状态。

## 2. 生产切换步骤引用

切换业务顺序以 [PRD.md 第 10 章](PRD.md) 为最高依据，上线前签收项以本文件第 1 节为准，
具体部署顺序见 [deploy/README.md](deploy/README.md)。执行摘要：

1. 冻结 tag/commit、镜像 digest、执行人和回退决策人；发布“停拉通知”，确保 T0 起旧直连系统不再调用 GetReport/GetReply。
2. 完成 24 件 secrets、厂商主备出口、加密备份隔离恢复、真实 LDAP 四角色和内网 Prometheus 七组指标验收。
3. 仅启动 `postgres redis migrate` 并核验 Alembic、分区、owner/app 权限；再启动 api、三个 worker、beat、web。生产不得启用 dev profile。
4. T0 起报告/回复拉取权唯一归平台；按 notice → verify → market 迁移，每个应用完成 API Key、回调/查询验证并观察 3 天。
5. 全部应用收口后由厂商重置旧密钥并更新 Docker secrets。归档 UAT、备份 SHA-256、监控截图与厂商回执。
6. 任一关键检查失败立即停止。单应用回退只恢复直连 Send，报告仍由平台代查；整体回退须先停平台轮询与全部发送 worker，再由安全负责人决定恢复直连。

## 3. BLOCKED 与降级清单

- `BLOCKED-D01`（仍生效）：asyncpg prepared statement 不支持在单次 `op.execute` 中执行整份多语句 `schema.sql`。安全降级为首迁移从唯一 `schema.sql` 读取全文，用内置解析器按顶层分号无损切分，并在同一事务逐条执行；重组逐字一致测试与 PostgreSQL 16 双空库结构比对均已通过。若必须坚持单次调用，需批准 migrate 专用同步驱动并重新安全评审。详见 [docs/DECISIONS.md](docs/DECISIONS.md)。
- `BLOCKED-D02`、`BLOCKED-D019`、`ENV-D03` 与基础镜像漏洞阻塞均已解除；真实四镜像结果为 API 0、Web 0、PostgreSQL 0、Redis 0。外部系统、受控仓库 RepoDigest 与人工验收仍按本清单执行，未完成前不得创建正式发布 tag。
- 其余 D001–D023 是已落地的保守工程决策；生产变更若与其冲突，先以 PRD 为准并补变更单，禁止静默绕过手机号、密钥、重复下发和审计红线。

## 4. 运维速查

常用检查与恢复入口：

```bash
sudo /usr/local/sbin/sms-compose config --quiet
sudo /usr/local/sbin/sms-compose up -d postgres redis migrate
sudo /usr/local/sbin/sms-compose up -d api worker-realtime worker-bulk worker-callback beat web
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/metrics
sudo /usr/local/sbin/sms-compose logs --since=15m api worker-realtime worker-bulk worker-callback beat
```

| 告警 | 含义 | 首要恢复动作 |
|---|---|---|
| `balance_low` / 厂商 99、999 | 余额低或双队列熔断 | 核对厂商余额并充值；确认无重复发送风险后由管理员执行队列强制恢复，观察 active chunk 与余额快照 |
| `vendor_error` / 鉴权、IP | 厂商密钥、出口白名单或服务异常 | 停止扩大流量，核对 SecretName/Key 与主备出口工单；不得打印凭据或盲目重发 uncertain |
| `uncertain` 超时 | Send 结果未知 | 仅通过 raw_vendor_log custom_ids 与厂商证据 reconcile；禁止自动或人工直接重发原 chunk |
| `callback_dead` | 回调五次仍失败 | 修复应用 URL/CIDR/接收端后从运维中心手动重推；核对签名时间窗与 callback_secret 版本 |
| `job_failed` | beat 任务连续失败 ≥3 | 查 job_run 错误摘要和对应 worker 日志，修复后在运维中心手动触发并确认新 success |
| `job_stalled` | 超过预期间隔 ×2 无心跳 | 检查单实例 beat 与 Redis 锁；确认旧 beat 已停止后只启动一个 beat，再验证 job_run 恢复 |
| `anomaly`（verify 为 crit） | 倍数与绝对量同时越阈 | 核查调用来源；必要时停用/轮换 API Key，确认无验证码轰炸，再调整阈值 |
| `unmatched` 增长 | 报告 customId 不属于平台事实 | 按号码/时间授权导出给旧系统对账，核对迁移期是否仍有直连 Send，保持平台唯一拉取权 |

数据库角色、审计归档、分区与备份恢复分别见 [deploy/dba.md](deploy/dba.md) 和 [deploy/backup-restore.md](deploy/backup-restore.md)。任何恢复都不得 UPDATE/DELETE `audit_log`，不得把手机号或 secret 输出到诊断记录。

## 5. 首月观察项

- 每日将 `stat_daily` 的消息数与计费条数和厂商账单逐应用、类别核对；成功率只按 `delivered/(delivered+failed)`，unknown/other 单列。差异先查分段、退订语、签名和 unmatched。
- 每周复核 anomaly 的倍数阈值、绝对量下限与 7 日同时段基线；调整必须保留双条件，verify 始终为 crit 并带处置建议。
- T0 起连续观察 unmatched、uncertain、poll lag 与 raw replay；目标是旧系统迁移完成后 unmatched 归零。未归零前不得重置或关闭对账证据。
- 每日检查七组 Prometheus family、job_run 心跳、回调 dead、Redis/数据库容量和分区创建；调度间隔修改后必须重启 beat。
- 每周抽查审计只增权限、手机号三列、OTP 等长打码、导出密文落盘与 7 天清理；发现 PII/secret 进入日志立即按安全事件停止相关链路。
