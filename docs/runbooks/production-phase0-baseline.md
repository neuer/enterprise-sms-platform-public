# 生产 Phase 0 决策基线与首发 Runbook

## 文档状态与授权边界

本文记录正式环境的已确认决策、残余风险、首发顺序和证据要求。它是 **Phase 0 文档基线**，
不是部署授权、生产就绪证明或运行配置。本文配套的文档契约测试只检查文字没有漂移；不会
创建 VMware 虚机、内部 Registry、数据库、Redis、VPN、告警渠道、生产 secret，也不会执行
发布、迁移、真实短信或故障切换。

配套 Phase 0 实现已经更新 settings、Compose、secrets、存储/TLS 预检和 release scripts；
这些静态制品及本地测试仍不等于生产基础设施已经部署或门禁已经通过。正式环境只能使用
当前精确 SHA 的受控入口，任何文档与可执行门禁不一致都必须先修复，不得靠手工命令或
伪造环境变量绕过。

## 已确认基线

| 领域 | Phase 0 决策 | 不能据此宣称 |
|---|---|---|
| 业务连续性 | 新平台替换旧短信能力但长期并行；双方使用不同服务商，首批应用可切回旧系统 | 切回旧系统不等于新平台恢复，也不证明数据完整 |
| 恢复目标 | 短信发送能力业务 RTO≤12h、平台数据 RPO≤24h；停服期间由上游积压，确认安全后补发 | RTO 从最早不可用/受控关入口计时；submitted/uncertain 禁止自动补发；旧系统恢复业务不等于新平台数据恢复 |
| 首发范围 | 仅 1–2 个低风险 `notice` 应用，每个应用观察至少 3 天 | 不能把首批成功外推为 verify、market 或全量应用验收 |
| 负载输入 | 约 50 万条/月，以单条请求为主，群发≤20 个号码，应用≤20 个 | 这是规划输入，不是压测、容量或性能证据 |
| 私有云 | VMware，两机房位于同一园区并由裸光纤互联；生产入口经 VPN | 同园区双机房不是异地灾备，VPN 可达不等于 TLS/授权边界合格 |
| 首发主机 | 主节点具备固定厂商出口；冷备和备出口暂非首发硬门禁 | 不能宣称主备切换、出口冗余或机房级连续性 |
| Redis | 与 Core/PostgreSQL 同处单台生产 VM，运行 broker/auth/control 三个显式 `isolated-standalone` 容器 | 三实例及整个平台都是单 VM 共同故障域；精确最终 SHA 和运行证据通过前不能称生产门禁已满足 |
| 数据 | 生产空库初始化；生产与测试数据、账号、API Key、25 件 canonical secrets（含 Redis ACL 密码）和备份口令完全隔离 | 不能复制测试库、测试 Key、测试手机号、Mock seed 或测试运行目录 |
| 保留期 | 业务消息/回复/号码明细 12 个月；审计 36 个月；raw/unmatched 90 天；导出 7 天；数据库加密备份 35 天 | 策略文档不证明清理、归档、备份或恢复任务已实际运行 |
| 告警 | 企业微信 + 公司邮件，均须有主接收人和替补 | `log-sink`、Mock 或单元测试不构成真实告警闭环 |
| 调用方积压 | 首批应用须持久保存至少 12 小时请求，24 小时内复用稳定 `biz_id`，新旧路由互斥且补发需审批 | 平台 RTO≤12h 本身不证明请求不丢、不重复或上游确有持久队列 |
| 制品 | 可新建内部 Registry；GitHub 连通不稳定；允许建设预生产 | Registry 尚未建设时不能宣称有内网制品供应链；预生产通过不等于生产已部署 |

## Phase 0 目标拓扑

```text
GitHub CI / 已验证候选
        │  四镜像 digest + manifest + SBOM + Trivy 证据
        ▼
受控制品提升节点 ─────────▶ 内部 Registry
                                  │ 同一 digest
                       ┌──────────┴──────────┐
                       ▼                     ▼
                  预生产环境             生产主节点
                                             │ 固定出口
VPN ─▶ 企业 TLS/入口 ────────────────────────┤──▶ 新短信服务商
                                             │
                                  ├─ PostgreSQL ── 每日加密备份（35 天）
                                  └─ Redis: broker/auth/control（三独立容器）

旧短信系统 ──旧服务商（长期并行，首批应用可受控切回）
```

内部 Registry、提升节点、预生产和生产资源均须以资产清单和真实读回为准。图中出现某组件只
表示计划关系，不表示已经创建或验收。

## 代码发布与更新

1. 选择一个不可变候选 commit，等待正式 CI/G2、四个最终镜像的独立 Trivy、SBOM、可重复
   构建和严格 manifest 证据完成。GitHub 短时不可达只能重试，不能把缺失证据解释为通过。
2. 由受控制品提升节点按 RepoDigest 将四镜像复制到内部 Registry；同步保存源 digest、目标
   digest、manifest 哈希、操作者和复核人。Compose、迁移和受控脚本的精确 commit 通过内网
   只读 Git 镜像 fast-forward 安装；生产不得直连 GitHub、源码构建或手工上传。内部 Git
   镜像与 Registry 缺一不可，二者都必须绑定同一候选 commit/manifest。
3. 预生产只拉取内部 Registry 中的同一组 digest，完成安装、迁移、认证、企微+公司邮件告警、
   备份恢复、旧系统切回和最小真实厂商链路演练。
4. 全新空主机只执行一次 `release bootstrap --manifest ... --confirm-empty-host` 建立首个
   succeeded 基线；后续使用 `release prepare/activate/status`。已成功发布的版本不能原地或
   直接切旧 digest 回退，须以新 commit、新 digest、保持当前 schema 的 forward-rollback 候选
   执行 `prepare-forward-rollback` 后再 activate。所有动作都必须经过 `deploy/sms-compose`；
   Phase 0 不授权 raw Compose，也不改变迁移失败时 schema 不自动回退的约束。
5. 常规发布可由唯一技术管理员执行；另一名具名的业务负责人或变更审批人
   以独立身份复核候选、变更窗、备份和回退条件，不要求其持有平台管理员账号，
   也不得共享管理员凭据。两个 ID 必须对应两名真实人员；无第二人可复核时为
   上线/恢复治理 No-Go，禁止同人使用两个 ID。身份与审批记录填写到 `[HANDOVER]`。
6. 每次更新先在预生产使用**将要激活的同一 digest**验收。生产失败只允许按正式状态机回退
   应用镜像；若涉及迁移、`recovery_required` 或 Redis 门禁冲突，保持 No-Go/停服处理。

## 生产初始化与隔离

1. 创建全新的生产数据库、数据卷、运行目录和备份目录；不得从公有云测试环境复制 volume、
   dump、用户表、手机号、消息、raw、审计或 Redis 数据。
   首次 bootstrap 前，七个固定 Compose bind 源目录、独立备份目录、`sms-platform`
   容器/volume 清单和 release 根必须全部为空；正式入口只读确认，不会自动删除或“修复”已有内容。
2. 在生产边界独立生成 25 件 canonical secrets（含三个 Redis ACL 密码）和另行保管的备份口令。新增
   `redis_tls_server_key` 只复制到 `current/redis`，不得进入 backend。只核对名称、版本、属主
   和权限，不把值写入命令参数、`.env`、Git、日志、工单或发布证据。
3. 仅空系统通过正式入口初始化首个本地管理员，但必须在 production bootstrap、首份生产加密
   备份及隔离恢复验真全部通过之后。变更复核的第二人是另一名具名的自然人，
   不是必须新建的第二个平台管理员账号。是否启用 AD 仍需
   生产 LDAP 地址、CA、bind secret、组映射和故障行为的独立验收，未确认时不得把 AD 写成
   首发已就绪。
4. 如需模板或非敏感配置，由业务负责人列白名单后在生产重新录入并审计。任何含 secret、PII
   或测试身份的导入包均拒绝。
5. 留存一份不含值的 test/prod 隔离清单：数据库标识、Registry namespace、VM/volume、网络段、
   secret generation ID、API Key generation ID、Redis 端口/目录/ACL 身份和备份路径。

## 服务器、Redis 与恢复准备

首发规格已经冻结，但仍须由 VMware 管理员提供真实资产和挂载读回，不能从文档推导为已分配：

| 资源 | vCPU/内存/磁盘 | 故障域与用途 | 证据 |
|---|---|---|---|
| 单台生产 VM | 12 vCPU / 48 GiB；VMDK 合计 1050 GiB | 同机承载 Core、Nginx、PostgreSQL 与三个 Redis 容器；整机为共同故障域 | VMware 资产、虚拟磁盘控制器、实际内存/CPU 读回 |
| 五个 VMDK | OS 100 / Docker 250 / PostgreSQL 400 / Redis 100 / Runtime 200 GiB | 固定 UUID 挂载；不得搬迁 Docker volume `_data` | `deploy/storage.md` 预检、fstab/findmnt/容量与权限证据 |
| PostgreSQL | 使用上述同机 400 GiB VMDK | 当前无托管 PostgreSQL；唯一事实源 | 版本、七角色、备份与隔离恢复报告 |
| 三 Redis 域 | 使用上述同机 100 GiB VMDK 的三个独立目录 | broker/auth/control 三实例；与整机及 Redis VMDK 共享故障域 | 三端点/目录/ACL/AOF/TLS 与整 VM 故障演练 |
| 应用预生产 | 规格可缩小，但服务、volume、secret 与发布合同必须一致 | 同 digest 发布和业务预验收；不安装生产恢复材料 | 资产读回及与生产差异清单 |
| 一次性隔离恢复机 | 从预生产资源池按需创建；空白、独立 PostgreSQL/VMDK、无生产发送出站 | 生产快照 full restore；证据归档后整机退役 | marker、恢复报告、退役记录 |
| 后续冷备 | 首发不配置 | 可按需创建；暂非首发硬门禁 | 资源申请时长与实际恢复耗时 |

Core、PostgreSQL 与三 Redis 合并在单台 VM 是已接受的风险模式。生产必须显式设置
`REDIS_HA_MODE=isolated-standalone`，由正式入口叠加 TLS/持久化合同，并以第 25 件 canonical
secret `redis_tls_server_key` 提供仅 Redis 服务端可读的私钥。精确最终 SHA 的 settings、
Compose、secrets、ACL/TLS、整 VM 故障恢复和 release evidence 未全部通过前，生产激活为
No-Go；禁止把 standalone 伪报为 managed。完整边界见
[deploy/redis-ha.md](../../deploy/redis-ha.md)。

旧系统受控切回并完成最小发送验收可以停止“短信发送能力业务 RTO”计时，但不能计作平台
RPO 或新平台恢复证据；新平台恢复耗时必须作为 `platform_recovery_elapsed` 另记。预生产通过
也不等于生产已部署。

当前无 KMS、统一监控和跨机房备份。首发至少要做到：备份口令置于仓库外 root-owned `0600`
持久文件；每日加密备份保留 35 天；生产 bootstrap 后、初始化管理员/API Key/T0 前，立即生成
首份生产快照并在与生产 PostgreSQL/VMDK 隔离的一次性空白恢复机完成恢复，记录恢复点年龄与
数据库恢复耗时；对主机、
容器、任务心跳、PostgreSQL、三个 Redis 域和业务告警建立可值守的采集与企微+邮件通知。上述
补偿措施不能被描述为 KMS、Redis HA 或跨机房灾备。Phase 0 的 metrics collector 位于生产 VM
宿主，通过固定 ingress 地址抓取 API，源 CIDR 收紧为网桥网关 `/32`；VM 整体不可达必须由
VMware/宿主外监控补充。生命周期和存储脚本只产出 journal 事件，外部链路负责送达企微和邮件。

## 首发与切回步骤

### T-3 天或更早

- 冻结首批 1–2 个低风险 notice 应用、负责人、新旧接口切换方式和观察指标。
- 在预生产用候选 digest 和预生产自有数据完成发布/迁移、备份恢复、告警、认证和旧系统切回演练；此时生产仍为空库，不得声称已有生产备份。
- 冻结每个应用的独立生产 API Key 申请、权限和交付方案；实际 Key 只能在首份生产备份及隔离
  恢复通过后生成，不迁移测试 Key。
- 对每个首批应用做停服/重启演练，证明 12 小时持久积压、稳定 `biz_id`、新旧路由互斥和补发审批；证据不足不得首发。
- 核对生产固定出口已被新服务商书面允许，且 VPN/TLS 入口只对批准来源开放。

### T0 激活

1. 复核 release manifest、内部 Registry digest、预生产恢复证据、Redis No-Go 已闭合和双人审批。
2. 全新空主机通过正式入口执行一次 `release bootstrap --confirm-empty-host`，记录 baseline
   release ID、迁移 head 和运行态读回后再启用 systemd；后续更新才执行 prepare/activate/status。
3. 在初始化管理员、创建生产 API Key 或开放流量之前，手动触发首份生产加密备份，校验 manifest
   和 SHA-256，再通过批准的离线通道把密文快照送到一次性空白隔离恢复机，按
   `deploy/failover.md` 的显式 snapshot/manifest 官方入口完成 full restore；不得复制或手改生产
   lifecycle state，也不得让预生产账本猜选候选。
   生产 VM 不启用 full restore drill timer，也不得在生产 PostgreSQL VMDK 创建演练库；共享应用
   预生产不得安装生产 recovery bundle、备份口令或 generation ID，也不得承担该演练。
   generation ID 只是非敏感标签，不是材料内容的摘要、MAC 或密码学绑定证明。离机 escrow
   必须按 manifest 所记 ID 原子发放不可变整包，由两名真实人员见证来源与一次性恢复机
   provision；历史 data AES/HMAC、四个 audit keys（按需含 alert pair）只能来自该整包。
   代码/报告的 ID 相等与 probe 成功只能证明当次已安装材料能读取其覆盖样本，不能证明 escrow
   provenance、所有历史行或未来仍可取得材料；该外部证据缺失即 T0 No-Go。
   该时点 `sms_message` 应为 0。schema v2 报告的
   `crypto_probe_receipts.pre_migration`/`post_migration` 必须各自严格覆盖 17 个固定密文字段，
   `counts.encrypted_rows=0`、全部 `coverage.rows=0`、`status=not_applicable_empty`；绑定 pre
   的 `checks.historical_ciphertext_validation` 也必须为 `not_applicable_empty`。这只构成
   初始化前空库门禁，不能证明未来生产历史密文可恢复。
4. 恢复证据通过后才初始化本地管理员、创建首批应用 Key；随后只开放首批应用，执行最小 notice
   发送并验证报告/回复、回调、审计和两个告警渠道。
5. 进入至少 3 天观察期；不接入 verify、market 或更多应用。

### 观察期

每天复核发送/计费条、失败、uncertain、Outbox、任务心跳、PostgreSQL、三个 Redis 域、备份
年龄、企微/邮件送达和新旧系统路由。证据不含手机号、HMAC、密钥、内部地址或原始日志。
首批最小 notice 产生至少一条 `sms_message` 后，须在 3 天观察期内再生成一份生产快照，并在新
的一次性空白隔离恢复机完成同一显式恢复；报告必须同时满足 `table_counts.sms_message>0`，
两份 receipt 对 17 字段精确闭集的逐字段逐实际版本样本均为 `performed`，且绑定检查为
`performed`。样本证据不得外推为所有历史行、alert 历史密文/keypair 或 `audit_log` MAC；
该证据未闭合前，不得扩大到更多应用、verify 或 market。

### 切回旧系统

1. 以最早业务不可用证据或受控关闭新入口时刻记录 `outage_start`；冻结首批应用的新请求和自动
   重试，关闭新入口并停用对应 API Key，确保调用方不会同时投向新旧路由。将以下**精确字段**
   写入 root:root `0600` 的 JSON（拒绝额外字段、链接和非 UTC 秒级时间）：

   ```json
   {"schema_version":1,"kind":"production_continuity_engage","evidence_id":"32位小写十六进制随机值","approved_at":"2026-08-24T01:30:00Z","outage_start":"2026-08-24T01:00:00Z","business_rto_seconds":43200,"old_system_fallback_allowed":true}
   ```

   `approved_at` 只能在未来 5 分钟容差内且不早于执行时刻 2 小时；不得把 `outage_start` 改写为
   围栏命令执行时刻。以文件原始字节的 SHA-256 执行
   `sudo deploy/sms-compose continuity engage --evidence /绝对路径/engage.json --evidence-sha256 <sha256>`。
2. 该命令在 lifecycle lock 内先把 intent 原子写入
   `/var/lib/sms-platform/continuity/state.json`，再停止并逐项读回
   `web/api/worker-realtime/worker-bulk/worker-callback/outbox-dispatcher/beat`；PostgreSQL 和三个
   Redis 数据服务保留。中途失败、宿主重启、状态或证据损坏都会继续阻断 `up`、systemd 启动、
   rotate、migrate、partition、init-admin 和所有 release mutation；用
   `sudo deploy/sms-compose continuity status` 只读取得机读状态，禁止删除或手改状态文件。
3. 对 queued/submitting/submitted/uncertain 逐类盘点。只有厂商与平台证据确认未受理的请求才可
   双人批准补发；submitted/uncertain 禁止自动重发。
4. 完成围栏读回后，才把互斥路由切到旧系统和旧服务商并完成最小 notice 发送验收。两个系统
   继续各自消费各自服务商的报告/回复。
5. 旧系统最小验收完成时记录 business-RTO 终点；新平台恢复继续单独记录
   `platform_recovery_elapsed`，且不能抵扣 RPO。
6. 只有旧路由已关闭、新路由唯一、在途已核对且 uncertain 明确禁止自动重发后，才可解除围栏。
   release evidence 必须是 root:root `0600` 精确 JSON，绑定 status 返回的 `fence_id`、原 engage
   SHA-256 和同一 `outage_start`，含新鲜 `approved_at`、变更记录 SHA-256、两个**不同**受控变更
   身份的不可逆 subject SHA-256，以及
   `approver_one_controlled/approver_two_controlled/old_route_disabled/new_route_exclusive/inflight_reconciled/uncertain_no_auto_resend`
   六个值全为 `true`。证据不得记录姓名、账号、手机号、请求内容或密钥。执行
   `sudo deploy/sms-compose continuity release --evidence /绝对路径/release.json --evidence-sha256 <sha256>`；
   管理器再次确认全部消费者停止后只把状态转为 `released`，**不会自动启动全栈**，后续仍须走
   受控发布/启动与最小 notice 验收。

## 首发 No-Go

满足任一项即停止激活或扩大范围：

- `isolated-standalone` 的精确最终 SHA、TLS/持久化 Compose、25 件 secret、三域 ACL 与整 VM
  故障恢复证据不完整，且没有三个 managed HA 替代端点；
- 生产与测试数据库、数据、secret、API Key、Redis ACL 或运行目录存在复用/来源不明；
- 没有可读回的固定主出口、VPN/TLS 边界或生产厂商书面白名单；
- 最近成功备份超过 24 小时、保留不足 35 天、首份生产备份未在隔离资源恢复验真，或业务回退
  从 `outage_start` 到旧/新平台最小验收超过 12 小时；
- 企微或公司邮件任一渠道仅为 `log-sink`、Mock、无人接收或无替补；
- 四镜像 digest、manifest、Trivy、SBOM、CI/G2 或内部 Registry 提升证据不完整；
- 预生产不是同一 digest，或发布/恢复/切回演练失败；
- 首批应用超过 2 个、不是低风险 notice、没有旧系统切回卡片，或观察期少于 3 天；
- 任一首批应用不能证明至少 12 小时持久积压、24 小时稳定 `biz_id`、新旧路由互斥或补发审批；
- RPO 缺口没有冻结调用方重试、厂商事实核对和未知结果禁止重发的 gap-fence；
- 只有 generation ID 相等或 probe receipt，没有离机 escrow 原子不可变整包、保留记录、
  两名真实人员见证来源和一次性恢复机 provision 的外部证据；
- 存在 unresolved `uncertain`、迁移失败、`recovery_required` 或其他正式发布门禁失败。

冷备、备出口和跨机房备份本身暂非首发 No-Go，但缺失状态必须留在变更单残余风险中，且
不得用“VMware 双机房”“裸光纤”或“旧系统可切回”包装成新平台灾备证据。

## 证据分级

| 证据 | 能证明 | 不能证明 |
|---|---|---|
| 本文与文档契约测试 | 决策、No-Go、保留期和边界没有文字漂移 | 资源存在、网络可达、任务运行或生产就绪 |
| 单元/Mock/静态检查 | 对应代码路径和契约在候选 commit 上通过 | 真实 LDAP、厂商、企微、公司邮件、容量或恢复 |
| CI/G2/Trivy/SBOM/manifest | 指定 commit/镜像候选满足对应构建与扫描门禁 | 镜像已进入内部 Registry或已部署生产 |
| 预生产同 digest 演练 | 候选在预生产的发布、迁移和业务场景结果 | 生产网络、数据、负载和外部系统完全相同 |
| `[HANDOVER]` 真实读回 | 指定时间、资产、release ID 下的生产事实 | 后续持续满足；必须按周期重新验证 |

所有 `[HANDOVER]` 证据只保存无敏感摘要、时间、状态和负责人，不保存 IP、域名、用户名、
手机号、密钥、token、原始日志或截图中的敏感内容。

## `[HANDOVER]` 首发记录

- 变更单、候选 commit、四镜像源/内部 digest 与 release ID：`[HANDOVER]`
- 生产/测试隔离清单及双人复核：`[HANDOVER]`
- VMware 资源、VPN/TLS、主出口和厂商白名单读回：`[HANDOVER]`
- Redis 生产模式闭合方式及整 VM 故障演练：`[HANDOVER]`
- 最近备份、35 天保留读回、隔离恢复点、实际数据 RPO、数据库恢复耗时：`[HANDOVER]`
- `outage_start`、冻结/围栏完成、旧或新平台最小验收、实际 business RTO 与独立
  `platform_recovery_elapsed`：`[HANDOVER]`
- 企业微信与公司邮件真实告警事件、主接收人和替补：`[HANDOVER]`
- 首批 1–2 个 notice 应用、切回卡片、T0 验收与至少 3 天观察结论：`[HANDOVER]`
- 残余风险、No-Go 例外（若有）及具名批准人：`[HANDOVER]`
