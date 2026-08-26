# 生产 Phase 0 威胁模型

## 范围与状态

本模型覆盖企业短信平台从公有云测试环境进入公司 VMware 私有云生产环境的 Phase 0：制品
提升、VPN 入口、承载 Core/PostgreSQL/三 Redis 的单生产 VM、固定厂商出口、企业微信/
公司邮件告警、备份恢复，以及与旧短信系统长期并行和切回。

这是设计期威胁模型，不是安全认证、渗透测试、生产部署或风险关闭证明。测试环境、生产环境、
旧短信系统和两个短信服务商均视为不同信任域；生产从空库初始化，任何测试数据或 secret
跨域复制都不在允许的数据流内。

## 资产与必须保持的安全属性

| 资产 | 必须保持的属性 |
|---|---|
| 手机号、消息、回复、raw/unmatched | 明文不持久化；只在授权边界受控解密；按 12 个月/90 天策略清理 |
| 审计日志 | 无手机号列表、只增不可改，保留 36 个月 |
| 厂商、JWT、加密/HMAC、DB、Redis、LDAP 等 secrets | 生产独立生成，只经受控文件挂载；不进入测试、Git、Registry、日志或命令行 |
| 发布制品 | 四镜像与 manifest 绑定不可变身份；预生产和生产使用同一候选。内部 Registry 建成前只允许签名的生产离线 Docker image archive 发布包（镜像 OCI-compatible，不是 OCI Image Layout） |
| PostgreSQL 事实 | RPO≤24h；加密备份保留 35 天并可在隔离环境恢复 |
| 发送唯一性 | timeout/网络异常保持 uncertain，submitted/uncertain 禁止自动重发 |
| 身份与权限 | VPN 不是身份认证替代；Web 仍显式 Provider、RBAC、step-up 和审计 |
| 告警 | critical 同时送达企业微信和公司邮件的具名主接收人/替补 |

## 信任边界与允许流

```text
GitHub/外部 CI ──候选证据/attestation──▶ 受控联网签名节点
                                             ├─签名封闭离线包──▶ 预生产──▶ 生产
                                             └─未来 RepoDigest──▶ 内部 Registry
VPN 用户/应用 ─▶ 企业 TLS 入口 ─▶ Web/API ─▶ PostgreSQL │ Redis 三域
                                      │                 │
                                      └─固定主出口──────▶ 新短信服务商
                                      └─受限出站────────▶ 企微/公司邮件

旧短信系统 ─────────────────────────────────────────────▶ 旧短信服务商
测试环境  ──X──▶ 生产数据库 / 生产 secrets / 生产制品信任根
```

允许旧系统与新平台长期并行，但不允许共享短信服务商凭据、API Key、数据库、报告拉取权或运行
secret。每个系统只能消费自己服务商的 GetReport/GetReply。

## 主要威胁、控制与残余风险

| 场景 | 攻击/失败路径与影响 | Phase 0 控制 | 残余风险与状态 |
|---|---|---|---|
| 测试污染生产 | 复制测试 dump/volume/Key/secret，使测试身份、手机号或弱凭据进入生产 | 生产空库；数据库、目录、制品空间、25 件 canonical secrets（含 Redis ACL 密码）、API Key 和备份口令独立生成并双人复核；`redis_tls_server_key` 只进入 `current/redis`，不进 backend | 未取得真实资产与 generation 读回前 No-Go |
| 制品供应链替换 | GitHub 不稳定时手工构建/裸上传，离线包被替换，或 Registry tag 漂移到未经验证镜像 | 四镜像 image ID+manifest+Trivy+SBOM+GitHub attestation；Ed25519 私钥不入仓库/生产，生产固定公钥与 key ID；导入前校验签名/摘要/大小，Docker 导入后逐镜像读回 ID/平台/labels；预生产/生产同包；生产不构建源码且禁止人工 `docker load` | 受控生成/签名环境、公钥安装、同包预生产和真实读回未闭合时 No-Go；内部 Registry 建成并演练后退出离线通道 |
| VPN 内部横向移动 | 已入 VPN 的非授权主体直达 Web、API、DB、Redis 或管理端口 | VPN 后仍由 TLS 入口、精确网段、Web/API 认证、RBAC、DB/Redis 网络隔离和审计约束 | VPN 自身、ACL 与防火墙规则需 `[HANDOVER]` 读回 |
| 单生产 VM 故障 | VM/宿主/维护使 Core、PostgreSQL、broker、auth、control 同时不可用；Redis VMDK 故障使三域同时丢失 | 显式 `isolated-standalone`；三实例保持进程、host:port、目录、ACL、密码和 AOF 隔离并强制 TLS；数据库每日加密备份；broker 由 Outbox 恢复，auth fail closed，control 写路径 503 | Core、数据库、三 Redis、宿主及 TLS 服务端私钥的共同故障域仍在；精确最终 SHA 和整 VM 恢复证据未闭合前发布 No-Go |
| PostgreSQL/主节点丢失 | 单主或本地备份同故障受损，平台数据不可用 | 平台数据 RPO≤24h：每日加密备份、35 天保留、manifest/SHA-256 与隔离恢复验真；短信发送能力 business-RTO≤12h 可由新平台恢复，或先完成发送围栏再受控切回旧系统并做最小验收结束 | 隔离数据库恢复仅验证工程恢复预算，其耗时作为 `platform_recovery_elapsed` 单列，不能冒充从 `outage_start` 计算的 business-RTO；无托管 PG、冷备和跨机房备份，不能声称机房级灾备 |
| 主出口丢失 | 新平台无法访问厂商，发送/报告停滞 | 固定主出口白名单；停服积压；必要时将首批应用切回旧系统 | 备出口暂非首发门禁；切回不恢复新平台数据或在途状态 |
| 新旧系统路由错误 | 同一业务重复提交、把未知结果在另一系统补发，或错误关闭新平台轮询 | 首批仅 1–2 个 notice；逐应用 Key；12 小时持久积压；稳定 biz_id；新旧路由互斥；切回前盘点 queued/sending/submitted/uncertain；不同服务商各自轮询 | 调用方路由与人工操作仍可能重复，需预生产停服/切回演练和至少 3 天观察 |
| 告警静默 | 无统一监控平台，critical 只写日志或外呼无人接收 | 企业微信+公司邮件双渠道、主接收人+替补、真实投递测试和升级记录 | 任一渠道仅 log-sink/Mock/无人接收即 No-Go |
| 无 KMS 的密钥窃取 | 宿主 root、备份或错误工单读取生产 secret/备份口令 | root-owned 0700/0600 文件、服务最小挂载、双人操作、值不进入证据；生产/测试完全隔离 | 宿主被完全攻陷时文件密钥可被窃取；不得声称 KMS/HSM 保护 |
| 保留期失效 | PII、raw、审计、导出或备份超期未删，或提前删除破坏审计/恢复 | 12个月/36个月/90天/7天/35天策略；任务记录、对象年龄、归档校验和恢复验证 | 文档与单元测试不证明生产清理已运行；需周期读回 |
| 备份勒索/篡改 | 攻击者同时改主库和同机备份，或替换 manifest | 加密备份、SHA-256 manifest、隔离恢复、最小权限 | 当前无跨机房备份；同园区/同管理域破坏仍可能同时影响源与备份 |

## 关键滥用与故障场景

### 场景 A：生产 VM 整体不可达

1. auth 域不可确认时登录、JWT 校验、refresh、step-up 全部 fail closed。
2. control 域不可确认时配额、频控、幂等或业务锁相关写路径返回 503，不把缺失投影当零。
3. 整机故障期间服务停止；从合格备份恢复 PostgreSQL 后，broker 事件继续以恢复出的 Outbox
   为事实，并按稳定 event ID 投递。旧系统切回不能替代新平台数据库恢复。
4. 不得把三个 standalone 交叉复用、临时启用 default 用户，或把单机形态标记为
   `REDIS_HA_MODE=managed`；必须显式使用 `isolated-standalone`。
5. 整 VM 恢复、Outbox backlog、会话拒绝、投影重建与 drift 必须成为演练证据。

### 场景 B：新平台发送超时后切回旧系统

1. 冻结该应用的新平台新请求，列出 queued/submitting/submitted/uncertain 状态，不包含手机号列表。
2. submitted/uncertain 继续按新服务商 customId 对账，禁止在旧系统自动补发。
3. 只有能够证明从未提交的请求，才由业务生成新的旧系统请求和新的业务幂等标识。
4. 记录两个系统的时间边界和人工复核，避免“停服积压后补发”演化为重复下发。

### 场景 C：发现生产 secret 来源于测试环境

立即 No-Go 或隔离生产入口；不通过改名或复制后重写来“洗白”。按 secret 类型独立生成、轮换、
吊销旧值，检查数据库、日志、Registry、备份、工单和浏览器存储暴露面，再以无敏感摘要复核。

## No-Go 与证据边界

以下任一项存在即不能激活或扩大生产范围：`isolated-standalone` 的精确最终 SHA、TLS/持久化
Compose、25 件 secret、三域 ACL 或整 VM 故障恢复证据未闭合；
生产/测试隔离来源不明；备份年龄或隔离恢复证据不满足平台数据 RPO，或业务回退从
`outage_start` 到旧/新平台最小验收超过 12 小时；受控 Registry RepoDigest 路径或临时生产离线
包的 manifest/签名/四 archive/同包预生产证据不完整；VPN/TLS/主出口未真实读回；企业微信或公司邮件未真实送达；首批范围、旧系统切回或
至少 3 天观察未落实；存在 migration/recovery/uncertain 等正式门禁失败。

- 文档契约测试只能证明这些文字存在。
- CI、G2、Trivy、SBOM 只证明指定候选的对应静态/构建门禁。
- 预生产只能证明同一 digest 在预生产的行为。
- 生产事实必须由 `[HANDOVER]` 的真实资产、网络、恢复、告警、release ID 和运行态读回证明。

冷备、备出口和跨机房备份暂非首发硬门禁，但其缺失必须持续列为残余风险。
**同园区双机房不是异地灾备**；不得因为两机房同园区且有裸光纤，就宣称已经消除了共同
物理/管理故障。
