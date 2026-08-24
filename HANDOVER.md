# 企业短信管理平台生产移交

> 本文件是 2026-07-15 的历史生产候选移交记录。当前日常流程以
> `MAINTENANCE.md` 为准，当前阻塞以 `PROGRESS.md` 为准。

2026-07-15 全库安全与前后端契约对齐、统一发布控制及远端 Mock 演练已合并并推送 `main`。仓库候选基线 `<redacted-commit>` 的 GitHub Actions run `<redacted-run>` 与 hosted Release Gate run `<redacted-run>` 均退出 0；已归档的本地 Trivy 0.70.0 发布门禁扫描 API、Web、PostgreSQL、Redis 四个最终镜像，均为 0 HIGH / 0 CRITICAL，并退出 0。P0 合并后的最终不可变证据归档到生产变更单与 release manifest，不再通过额外文档提交回填 SHA/run ID。生产 Phase 0 已把首发边界改为本地认证、单主 + 旧系统回退的业务 RTO≤12h、平台数据 RPO≤24h、主出口、每日备份和 1–2 个 notice 应用；真实 AD、冷备/备出口及 24 小时压测均为后续增强，不再是本次首发硬门禁。当前执行以 [生产 Phase 0 Runbook](docs/runbooks/production-phase0-baseline.md) 为准。

> M4.1 前后端页面对齐补丁已在全新 Mock 卷重新执行 G2，并完成四角色、PRD 18 页面形态和 390px 真实浏览器验收。生产候选必须包含 M4.1 最终交付提交；旧 `m4` 镜像不含该补丁。

## 1. 需人工完成清单

- [x] 代码基线 `<redacted-commit>` 已执行 `bash scripts/verify_release.sh`：API 0、Web 0、PostgreSQL 0、Redis 0；当前仓库候选 `<redacted-commit>` 的 CI run `<redacted-run>` 与 hosted Release Gate run `<redacted-run>` 全部成功。v1.6.14 四镜像/数据持久化候选 `<redacted-commit>` 的证据继续有效。生产仍须把四镜像推送到受控仓库并归档扫描报告、扫描器 digest 与最终镜像 digest（RepoDigest）；P0 最终 SHA 与 run ID 按本节开头的证据边界处理。
- [ ] 配置外部 TLS 终结器的证书、HTTPS 重定向与 HSTS（`max-age>=31536000; includeSubDomains`），按部署手册归档 curl 结果；内部 HTTP Nginx 不设置 HSTS。真实浏览器确认 CSP/Permissions-Policy 生效且控制台无 CSP violation。
- [ ] 完成一次性 production `release bootstrap --confirm-empty-host`，立即生成首份生产加密备份并在从预生产资源池按需创建的一次性空白隔离恢复机验真；共享应用预生产不得安装生产恢复材料或承担该演练。通过后才执行 `sms-compose init-admin --show-temporary-password` 初始化本地管理员并完成首次改密。首发只启用 local Provider；AD 待地址、CA、bind secret 和组映射齐备后另行验收。生产设置 `DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`，禁止运行 `seed-dev`。
- [ ] 一次性恢复机先停止平台和 lifecycle timers，然后安装完整 25 件 canonical secrets：
  离机 escrow 按 manifest 所记 generation ID 原子发放、不可变且双人见证的 data AES/HMAC+
  四个审计 key（按需再含 alert pair），以及当前获批的
  其余运行凭据；另安装 backup passphrase、两个固定 ID 和精确 marker。只通过
  `release start-recovery` 生成/绑定 runtime generation 后调用官方 `restore_drill.py`；禁止普通
  `up`、通用 `exec`、手工 prepare 或修改 `/run`。详细命令以 [deploy/failover.md](deploy/failover.md) 为准。
- [ ] generation ID 只是标签，不是密钥/口令内容的摘要、MAC 或密码学绑定证明；两名真实人员
  已见证 escrow 整包来源与一次性恢复机 provision，且 probe 实际成功。报告只能证明已安装
  材料可读取其覆盖样本，不能证明 escrow provenance、所有历史行或未来可用性；缺任一外部
  见证/保留证据即上线或恢复 No-Go。
- [ ] 由受控密钥系统向生产主节点独立落盘 25 件生产运行 secret：厂商两项、AES/HMAC、
  主体 1 个与 API/realtime/bulk 3 个自治审计 HMAC（共 4 个）、企微 X25519 公私钥对、JWT、
  LDAP、metrics、`db_owner_password`、七个 `db_<role>_password`、三个 Redis ACL 密码和
  `redis_tls_server_key`；目录 0700、文件 0600，DB owner secret 只挂载 postgres/db-role-provision/migrate，TLS 私钥只进入 Redis runtime。
  按 [deploy/secrets.md](deploy/secrets.md) 双人复核，不复制 secret 值到 `.env`、工单、
  日志或聊天。
- [ ] 向厂商报备生产主出口固定 IP，书面确认真实 QPS、单次号码上限及生效时间并验证 GetBalance。将确认值写入 `sys_config.vendor_qps`、`reserved_realtime_qps` 与 `vendor_batch_size`，重启 beat 使调度配置生效。备出口为后续增强，不阻断首发。
- [ ] 在 `sys_config` 配置经审批的企业微信 webhook、SMTP relay/发件人与收件人；触发无 PII 测试告警并确认 `alert_log`、企微和邮件均收到。未配置前系统只使用 log-sink。
- [ ] 按 [deploy/failover.md](deploy/failover.md) 验证业务回退 RTO≤12h、每日加密备份、35 天保留和隔离恢复；分别记录 `outage_start`、旧系统或新平台恢复发送的业务 RTO、平台数据 RPO 和 `platform_recovery_elapsed`。冷备未建成时记录残余风险，不得宣称主备切换；恢复期间禁止新平台同一凭据出现双 beat、双报告轮询或双发送 worker，旧系统继续只轮询其旧服务商并可并行。
- [ ] 当前 dedicated live production restore action 尚未封口；只允许一次性隔离恢复验证，
  真实新平台 live 恢复仍为 No-Go。事故时必须先冻结/围栏新平台，再切回不同服务商
  的旧系统；禁止通用 `exec`、手工 `pg_restore`、普通 `up`、raw Compose 或手改
  `/run`/release state 拼接生产恢复。
- [ ] 首份 pre-T0 空库恢复只接受 `table_counts.sms_message=0`，且 schema v2
  `crypto_probe_receipts.pre_migration`/`post_migration` 各自严格覆盖 17 个固定密文字段、
  `counts.encrypted_rows=0`、全部 `coverage.rows=0`、`status=not_applicable_empty`；绑定 pre
  的汇总检查也必须为 `not_applicable_empty`，并明确标为初始化前门禁。首批最小 notice
  后、3 天观察期内再以一份生产快照在新的一次性空白隔离恢复机复验，必须得到
  `table_counts.sms_message>0`，两份 receipt 的逐字段逐实际版本样本及绑定检查均为
  `performed`；抽样不得外推为所有历史行、alert 历史密文/keypair 或 `audit_log` MAC，
  否则不得扩大到更多应用、verify 或 market。
- [ ] 24 小时、100000 条 Locust 长稳压测已延期，不作为本次首发硬门禁；仍须完成与 50 万条/月规划输入相称的预生产容量冒烟并记录缺失证据，扩大到 verify/market 或更多应用前再完成 [docs/PERFORMANCE.md](docs/PERFORMANCE.md) 的正式长稳测试。
- [ ] 在预生产完成 [docs/UAT.md](docs/UAT.md) 的 28 例适用验收；生产首发真人 UAT 仅覆盖
  `AUTH_MOCK=0` 的 local Provider、首批 notice 应用及对应安全/运维边界。AD 登录与角色映射
  延后到 AD Provider 正式启用前独立复验。自动化 20 项历史证据保存在受限归档，不随公开快照发布。
- [x] 已在隔离主机完成一次**远端 Mock 发布演练**：精确修复提交 `<redacted-commit>` 覆盖 Web-only、API-only、数据镜像验证、配置失败不变、健康失败补偿与 TERM/resume，最终退出 0；恢复后默认 10 个容器 ID 与 4 个卷在最终演练前后逐项一致，公网边界和管理员登录/退出浏览器冒烟通过。首次执行误清理默认 Mock 卷、测试环境重置及整改证据保存在受限归档，不随公开快照发布；不得描述为原测试数据未变化。`release_control_smoke` 只证明控制面，不代表发布就绪，也不替代正式 Trivy `release` 证据。
- [ ] 为生产统一四镜像发布建立**生产变更单**：绑定 release_id、commit、四个 RepoDigest/image ID、changed subset、迁移兼容性、维护窗口、Trivy/数据/备份恢复证据、执行/复核/回退人和终态；不得写 secret 或手机号。
- [ ] 变更执行人可为唯一技术管理员；复核人必须是另一名具名的业务负责人或变更
  审批人，不要求其持有平台管理员账号，也不得共享凭据。两个 ID 必须对应两名真实人员；
  无第二人可复核时为上线/恢复治理 No-Go，禁止同人使用两个 ID。
- [ ] 变更关闭前保留发布包、旧镜像和事件记录，包括 `state.json`、`events.jsonl`、`residual_changes` 与上一成功回退点；发布工具不自动 prune。`recovery_required` 时禁止自动续跑、清理或猜测迁移状态。

## 2. 生产切换步骤引用

切换业务顺序以 [PRD.md 第 10 章](PRD.md) 为最高依据，上线前签收项以本文件第 1 节为准，
具体部署顺序见 [deploy/README.md](deploy/README.md)。执行摘要：

1. 冻结 tag/commit、镜像 digest、执行人和回退决策人；确认新旧系统只轮询各自服务商与各自凭据，任何一方都不得接管另一方的 GetReport/GetReply。
2. 完成 25 件 secrets、主出口、候选在预生产的隔离恢复、企微+公司邮件和基础监控先决条件；AD、备出口和冷备状态按 Phase 0 残余风险记录。此时生产仍为空库，不得声称已有生产备份。
3. 不手工拆分启动服务；全新空主机用正式 baseline manifest 执行一次 `release bootstrap --confirm-empty-host`，核验 Alembic、分区、owner/app 权限和终态，随后启用 systemd。立即生成首份生产加密备份，并在一次性空白隔离恢复机完成恢复验真；只有该证据通过后，才初始化本地管理员、创建 API Key 或进入 T0。生产不得启用 dev profile。
4. T0 只接入 1–2 个低风险 notice 应用；每个应用完成 API Key、报告/回复、回调/查询验证并连续观察至少 3 天。verify、market 与更多应用须另行审批，不属于本次首发范围。
5. 旧系统保持长期并行；只有后续明确完成全部应用收口时，才由厂商重置旧系统密钥。归档 UAT、备份 SHA-256、监控截图与厂商回执。
6. 任一关键检查失败立即停止。应用切回后，旧系统负责旧服务商的新发送与报告；新平台继续完成自己已提交批次的报告/对账，但不再接收该应用新请求。只有新平台自身无未终结批次且有书面处置时才停止其轮询。

## 3. BLOCKED 与降级清单

- `BLOCKED-D01`（仍生效）：asyncpg prepared statement 不支持在单次 `op.execute` 中执行整份多语句 `schema.sql`。安全降级为首迁移从唯一 `schema.sql` 读取全文，用内置解析器按顶层分号无损切分，并在同一事务逐条执行；重组逐字一致测试与 PostgreSQL 16 双空库结构比对均已通过。若必须坚持单次调用，需批准 migrate 专用同步驱动并重新安全评审。详见 [docs/DECISIONS.md](docs/DECISIONS.md)。
- `BLOCKED-D02`、`BLOCKED-D019`、`ENV-D03` 与基础镜像漏洞阻塞均已解除；真实四镜像结果为 API 0、Web 0、PostgreSQL 0、Redis 0。外部系统、受控仓库 RepoDigest 与人工验收仍按本清单执行，未完成前不得创建正式发布 tag。
- 其余 D001–D023 是已落地的保守工程决策；生产变更若与其冲突，先以 PRD 为准并补变更单，禁止静默绕过手机号、密钥、重复下发和审计红线。

## 4. 运维速查

常用检查与恢复入口：

```bash
sudo /usr/local/sbin/sms-compose config --quiet
sudo /usr/local/sbin/sms-compose release bootstrap --manifest /secure/staging/production-baseline/manifest.json --confirm-empty-host
sudo /usr/local/sbin/sms-compose release prepare --manifest /secure/staging/release-20260824/manifest.json
sudo /usr/local/sbin/sms-compose release activate --release-id release-20260824
sudo /usr/local/sbin/sms-compose release status --release-id release-20260824
curl -fsS http://localhost:8000/livez
curl -fsS http://localhost:8000/readyz
# /metrics 由 deploy/prometheus.example.yml 的宿主本地 collector 携 Bearer 抓取，禁止裸 curl 或把 token 放进命令参数。
sudo /usr/local/sbin/sms-compose logs --since=15m api worker-realtime worker-bulk worker-callback beat
```

| 告警 | 含义 | 首要恢复动作 |
|---|---|---|
| `balance_low` / 厂商 99、999 | 余额低或双队列熔断 | 核对厂商余额并充值；确认无重复发送风险后由管理员执行队列强制恢复，观察 active chunk 与余额快照 |
| `vendor_error` / 鉴权、IP | 厂商密钥、出口白名单或服务异常 | 停止扩大流量，核对 SecretName/Key 与当前启用出口工单；不得打印凭据或盲目重发 uncertain |
| `uncertain` 超时 | Send 结果未知 | 仅通过 raw_vendor_log custom_ids 与厂商证据 reconcile；禁止自动或人工直接重发原 chunk |
| `callback_dead` | 回调五次仍失败 | 修复应用 URL/CIDR/接收端后从运维中心手动重推；核对签名时间窗与 callback_secret 版本 |
| `job_failed` | beat 任务连续失败 ≥3 | 查 job_run 错误摘要和对应 worker 日志，修复后在运维中心手动触发并确认新 success |
| `job_stalled` | 超过预期间隔 ×2 无心跳 | 检查单实例 beat 与 Redis 锁；确认旧 beat 已停止后只启动一个 beat，再验证 job_run 恢复 |
| `anomaly`（verify 为 crit） | 倍数与绝对量同时越阈 | 核查调用来源；必要时停用/轮换 API Key，确认无验证码轰炸，再调整阈值 |
| `unmatched` 增长 | 新平台服务商返回的 customId 不属于平台事实 | 保留加密记录，核对异常外部调用、恢复缺口或服务商侧事实；旧系统使用另一服务商，不向其导出或混合对账 |

数据库角色、审计归档、分区与备份恢复分别见 [deploy/dba.md](deploy/dba.md) 和 [deploy/backup-restore.md](deploy/backup-restore.md)。任何恢复都不得 UPDATE/DELETE `audit_log`，不得把手机号或 secret 输出到诊断记录。

## 5. 首月观察项

- 每日将 `stat_daily` 的消息数与计费条数和厂商账单逐应用、类别核对；成功率只按 `delivered/(delivered+failed)`，unknown/other 单列。差异先查分段、退订语、签名和 unmatched。
- 每周复核 anomaly 的倍数阈值、绝对量下限与 7 日同时段基线；调整必须保留双条件，verify 始终为 crit 并带处置建议。
- T0 起连续观察 unmatched、uncertain、poll lag 与 raw replay；目标是新平台服务商凭据下没有未知 customId。未归零前不得重置或关闭对账证据，也不得归因给使用另一服务商的旧系统。
- 每日检查十二组 Prometheus family、job_run 心跳、回调 dead、Redis/数据库容量和分区创建；调度间隔修改后必须重启 beat。
- 每周抽查审计只增权限、手机号三列、OTP 等长打码、导出密文落盘与 7 天清理；发现 PII/secret 进入日志立即按安全事件停止相关链路。
