# 企业短信管理平台 PRD

| 项 | 内容 |
|---|---|
| 文档版本 | v1.6 |
| 日期 | 2026-07-06 |
| 状态 | 已评审定稿 |
| 上游依赖 | 智慧信息-企信版短信网关（vendor.example.invalid，接口文档 V2.1.2） |
| 开发方式 | 维护期按 MAINTENANCE.md / AGENTS.md 协作，契约由 openapi.yaml / schema.sql 约束 |

## 变更记录

| 版本 | 变更 |
|---|---|
| v1.0 | 初版 |
| v1.1 | 消息三分类、幂等、可靠性(uncertain/对账/raw落地)、字段级加密、结果回调、双Key轮换、失败重发、审批回避、登录防护、审计只增 |
| v1.2 | ①**计费条核算**（配额/预估/报表全按计费条） ②**冷备节点**（RTO≤30min） ③**号码级防轰炸频控** ④模板变量机制（平台渲染） ⑤营销合规（退订语+同意留痕） ⑥测试发送 ⑦审计防PII ⑧导入边界 ⑨补齐Web查询/队列恢复/用户管理/回调密钥轮换端点 ⑩前端页面清单附录 |
| v1.3 | **交付包补齐（需求不变）**：新增 docs/vendor-api.md（厂商精确报文与 mock 契约）、BOOTSTRAP.md（Claude Code 开工引导+Kickoff Prompt+首管引导）、deploy/docker-compose.yml+.env.example+seed.example.sql；修正模板变量厂商格式对齐（平台 `{n}` ⇄ 厂商 `{s长度}`，模板增加 var_specs） |
| v1.4 | ①**上线切换与并行期方案**（第10章：报告拉取权一刀切+unmatched 报告保留+回退预案） ②**verify 类 OTP 等长打码**（默认开，OTP 不落库） ③成功率口径统一（unknown 不入分母） ④ops 支撑端点补齐（告警/raw列表/uncertain列表/unmatched） ⑤docs/UAT.md 验收用例集 ⑥M4 增压测任务 ⑦契约回填与认证方式规则 |
| v1.5 | **三个新功能（不改既有设计）**：①FR-25 发送量异常检测（基线×3σ 突增告警，密钥盗刷/接入方 bug 兜底） ②FR-26 号码时间线（单号码收发全history，投诉核查一屏还原） ③FR-27 后台任务健康（job_run 记录+心跳缺失告警+看板，消除 beat 静默挂掉盲区）；docs/ 收编 UI 设计稿两件套 |
| v1.6 | **无人值守执行层与规格闭环**：AUTOPILOT.md（DONE 定义/分层门禁/3次失败 BLOCKED/跨会话续跑）、AUTH_MOCK、告警 log-sink、消歧规则 31–37、owner/app 双角色、敏感中间产物加密、可过期 DB 幂等、完整 OpenAPI/Mock/UAT、[HANDOVER] 人工移交标记 |
| v1.6.1 | **P0 账号体系重构**：内置本地账号与 AD Provider 并存、登录显式选择认证源、首个本地管理员独立初始化、首次强制改密、稳定主体/身份/凭据分层模型、AD 草稿测试激活工作流；未来 IAM Provider 仅预留扩展能力，本期不实现 |
| v1.6.4 | **事务性可靠发布**：业务状态与无 PII `outbox_event` 在同一 PostgreSQL 事务提交；独立 dispatcher 以租约/fencing 发布，Celery task ID 固定为 event ID，消费者按事件 ID 幂等；broker 首次失败不改变 HTTP 已受理语义，恢复扫描仅作兜底 |
| v1.6.5 | **可恢复用量事实**：配额和号码频控改由 PostgreSQL 事实账本串行判定，Redis 仅保存版本化绝对投影；终态释放经事务性 Outbox 幂等补偿，支持 HMAC 轮换归并、漂移告警、无 PII 解释和安全重建 |
| v1.6.6 | **Redis 故障域硬化**：Celery broker、认证会话、业务控制面拆为三个独立高可用端点和 ACL 身份；密码只经 Docker secrets，认证故障 fail closed，broker/control 分别由 Outbox 与 PostgreSQL 事实恢复 |
| v1.6.40 | **安全日报配置 UI 化**：管理员页面配置 Resend Key、收件人和启停状态，API 同步独立 mailer 配置文件；不再要求手工维护 Docker secret 与收件人文件 |

---

## 1. 背景与目标

### 1.1 背景
集团各业务系统（ERP、OA、监控告警、IAM 等）均有短信发送需求，目前分散对接或人工操作，存在以下问题：

- 无统一入口，厂商密钥（SecretName/SecretKey）散落在多个系统，泄露面大
- 无配额与审批管控，误发/滥发无法约束；无防轰炸机制，验证码接口可被滥用骚扰他人
- 无统一的发送记录、状态报告与审计，不满足等保 2.0 审计要求
- 余额耗尽、发送失败无人感知；长短信按计费条扣费，成本无法预估与对账
- 验证码与营销短信混发，互相影响时效与体验；营销发送无合规留痕

### 1.2 目标
建设统一的企业短信管理平台，作为集团与短信网关之间的**唯一通道**：

1. 内部系统通过平台统一 API（API Key 认证）发送短信，厂商密钥仅保存在平台侧
2. 运维/业务人员通过 Web 界面显式选择本地或 AD 认证源登录，进行人工发送、审批、查询与管理
3. 按消息类别（验证码/通知/营销）实施差异化的队列、时间窗、黑名单、审批与号码级频控策略
4. 以**计费条**为统一计量单位实施配额、成本预估与报表，与厂商账单可对账
5. 提供内容管控、告警、结果回调、审计与营销合规留痕能力
6. 手机号等个人信息加密存储，满足 PIPL 与等保 2.0 要求

### 1.3 非目标（本期不做）
- 语音发送、国际短信
- 个性化变量群发（Web 端每行不同内容）——二期；本期 API 单发天然支持个性化
- 上行回复的自动化业务处理（仅采集、展示、查询、退订加黑）
- 多厂商网关路由（架构预留适配层）
- IAM Provider 对接（认证源接口与数据模型已预留，**仅预留扩展能力，本期不实现**）
- 双活/K8s 高可用（本期为冷备方案）

## 2. 名词定义

| 名词 | 定义 |
|---|---|
| 应用（App） | 平台的 API 接入方，每个应用一对 API Key，归属某部门 |
| 消息类别 | verify 验证码 / notice 通知 / market 营销 |
| 批次（Batch） / 分片（Chunk） / 消息（Message） | 一次请求 / 一次厂商 Send 调用 / 单个手机号明细 |
| **计费条（Segment）** | 运营商计费单位：最终内容（含签名与退订语）≤70 字计 1 条，超过按 67 字/条向上取整。**配额、预扣、余额预估、报表均按计费条核算** |
| 状态报告 / 上行回复 | 厂商 GetReport / GetReply 拉取数据 |
| 结果回调 | 平台向应用推送结果的 Webhook |
| 测试发送 | ≤test_send_max（默认5）个号码的试发，豁免营销时间窗，其余管控不豁免 |
| 认证源（Provider） | 可独立完成身份认证的来源；本期实现内置 local 与可配置 AD，未来 IAM Provider 仅预留扩展能力，本期不实现 |
| 平台主体 / 外部身份 / 凭据 | `user_account` 是稳定授权主体，`auth_identity` 绑定认证源身份，`local_credential` 仅保存本地 Argon2 密码摘要与强制改密状态 |

## 3. 用户与角色

| 角色 | 来源 | 权限 |
|---|---|---|
| 系统管理员 | 本地维护 / AD 组映射或人工覆盖 | 全部功能，含账号维护、用户角色手动覆盖、队列恢复、强制下线 |
| 审批人 | 本地维护 / AD 组映射或人工覆盖 | 审批（不能审本人提交）；查看全量记录与报表；导出任务对象按 FR-16 限制为本人/同部门 |
| 操作员 | 本地维护 / AD 组映射或人工覆盖 | Web 人工发送（notice/market）、本部门记录、本部门模板 |
| 只读用户 | 本地维护 / AD 组映射或人工覆盖 | 本部门记录与报表 |
| 接入应用 | API Key | 发送/查询 API，受 allowed_categories、配额、限流、频控约束 |

- Web 登录必须**显式选择认证源**并提交 `provider_code`；服务端只调用所选 Provider，**禁止自动回退**到其他认证源。所有 Provider 共享账号锁定（5次/15min）、IP 限流（5min 内失败≥20 封 15min）和统一错误话术。访问 JWT 固定 15 分钟，refresh JWT 最长 7 天且每次使用后由 Redis Lua 原子单次轮换；并发重放会吊销该 refresh family。
- 每次访问 JWT 验证都必须读取数据库权威投影，并逐项匹配稳定 `account_id`、`identity_id`、Provider、登录名、部门、角色和统一 `security_version`，同时要求账号、身份及 Provider 均启用。账号/身份状态、部门、角色/覆盖、外部组映射、Provider 启停/生效配置、密码重置和强制下线的事务必须递增 `security_version`，使旧 access/refresh JWT 立即 401；数据库或 Redis 吊销/轮换状态不可用时返回 `AUTH_SESSION_UNAVAILABLE`/503 并 fail closed。
- 平台使用**全局不区分大小写登录名空间**，用户名规范化后唯一，归属按**先到先得**确定。本地账号创建时不探测 AD；后续真实 AD 登录若与既有本地身份冲突，则拒绝并审计 `ACCOUNT_SOURCE_CONFLICT`，由管理员线下处理。
- 平台**不开放自助注册**，本地账号仅由管理员维护；管理员可创建、启停、重置临时密码、设置角色与强制下线，但不得重命名或硬删除账号。禁止停用自己，且不得停用或降级最后一个有效管理员。
- 本地密码为 12–128 位，大小写字母、数字、特殊字符至少三类，不能包含用户名；管理员创建/重置后均标记为临时密码，用户**首次登录必须修改密码**。首次改密令牌只以哈希写入 PostgreSQL，并与主体、认证源、用途和签发时安全版本绑定；令牌消费、密码更新、`must_change_password` 清除、`security_version` 递增及审计必须在同一事务完成，数据库失败时全部回滚且令牌可安全重试。密码与用户名规则在登录、改密和用户管理界面提交前可见。
- Web 会话保持 Bearer Header 契约：普通 access/refresh JWT 仅保存在当前标签页 `sessionStorage`，注销、401、会话超时和强制下线后必须清理，并用不含凭据的浏览器存储事件同步其他标签页。首次改密、导出 step-up 等高风险短期令牌只允许存在于发起操作的组件局部易失内存，不得进入 Pinia、浏览器存储、URL、日志或 DOM 持久节点；组件销毁或到期立即清空。生产入口的 HTTP 只能跳转到同一受控 HTTPS origin，最终响应必须满足 TLS 1.2+、HSTS、收紧后的 CSP 与证书到期监控门禁。
- 首个管理员由空系统执行 `sms-compose init-admin --show-temporary-password` 创建，与 AD 完全无关；命令生成 20 位临时密码并在当前 TTY 一次显示，可由 Codex 通过 PTY 代为执行并转告操作者。
- AD 默认禁用；管理员登录后在**系统配置页**维护非敏感草稿，依次保存、测试当前版本并激活。Bind 密码仍仅来自 Docker secret，CA 仅来自受控文件。AD 角色默认由组映射计算，允许管理员单人覆盖或恢复跟随。

## 4. 总体架构

```
内部系统 ──API Key──▶ ┌────────────────────────────────────┐
                      │  FastAPI 应用层                     │
Web(Vue3) ──JWT────▶  │  认证/RBAC │ 发送流水线 │ 管理 │ 报表 │
                      └──────┬──────────────────┬──────────┘
                             │PostgreSQL Outbox dispatcher ─▶ Redis broker/Celery
                             │Redis auth(会话/撤销/step-up) + control(配额/频控/锁/投影)
              ┌──────────────┼──────────────┐   ▼
              ▼              ▼              ▼  PostgreSQL(唯一事实源,字段级加密)
        Celery worker   Celery worker   Celery beat(单实例)
        队列: realtime   队列: bulk      轮询/调度/对账/清理/聚合
              └──────┬────────┘   Celery worker 队列: callback
                     │ 网关适配层(唯一持有厂商密钥, Docker secrets)
                     ▼
            智慧信息-企信版 (vendor.example.invalid)

主节点 ──每日 pg_dump + 配置/secrets 同步──▶ 冷备节点(预装同版本Compose, RTO≤30min)
```

- **发送流水线**（同步阶段）：参数校验 → 类别策略装载 → 模板渲染 → 营销合规处理（退订语/同意校验）→ **计费条计算与配额预估** → 幂等检查 → 去重 → 黑名单过滤 → **号码级频控剔除** → 敏感词检查 → 时间窗判定 → 配额预扣（按计费条）→ 审批判定 → 批次与 `batch.ready` Outbox 事件原子入库
- **可靠性**：PostgreSQL 唯一事实源；独立 Outbox dispatcher 使用 `FOR UPDATE SKIP LOCKED`、唯一 event ID、租约/fencing、指数退避和 dead-letter；Celery task ID 固定为 event ID，消费者按事件 ID 领取执行租约。数据库提交成功即为确定受理，broker 首次失败不回滚或伪装成未受理；beat 单实例和 reconcile 只保留审计/兜底；冷备节点见 NFR-02

## 5. 功能需求

### 5.1 消息分类与频控

#### FR-00 类别策略矩阵（核心）
所有发送必须归属一个类别（阈值/窗口/频控均 sys_config 可配，应用级可覆盖频控）：

| 策略项 | verify 验证码 | notice 通知 | market 营销 |
|---|---|---|---|
| 队列 | realtime（高优先） | realtime | bulk |
| 发送时间窗 | 不限 | 不限 | 默认 08:00–21:00，窗外自动转次日窗口起点定时并告知 |
| 黑名单 | 不拦截 | 默认拦截，应用可关 | 强制拦截 |
| **号码级频控（v1.2）** | 同号码 **1条/分钟、10条/天**（全局跨应用） | 默认不限 | 同号码同应用 **1条/天** |
| 审批（仅 Web） | 不适用（仅 API） | ≥ approval_threshold（默认100） | ≥ market_approval_threshold（默认50） |
| 营销合规 | — | — | 内容强制含退订语（缺失自动追加"回T退订"，可配关闭）；Web 提交须勾选"已获用户同意"并记审计 |
| 敏感词/去重/配额/审计 | 全类别一致执行 | 同左 | 同左 |
| 厂商 QPS | realtime 预留 reserved_realtime_qps（默认2） | 同左 | 仅用剩余额度 |

- 频控超限号码在受理阶段剔除，计入 `removed_freq_limit` 返回；全部被剔则 422 ALL_FILTERED
- 应用 `allowed_categories` 越权返回 403；Web 只能选 notice/market

#### FR-00a 计费条核算（v1.2 新增，核心）
- 计费函数（唯一实现 services/billing.py）：`segments = 1 if L ≤ 70 else ceil(L / 67)`，L = 最终下发内容长度（**含签名与退订语**，汉字/字母/数字均计 1 字符）
- 应用范围：配额预扣与回补 = 受理号码数 × segments；余额消耗预估；报表与仪表盘同时展示消息数与计费条数
- 受理响应返回 `est_segments`（单条计费条数）与 `quota_cost`（本批次配额消耗）；Web 提交前实时预览"预计消耗 X 计费条"
- 对账：stat_daily 记录计费条汇总，与厂商月账单核对

### 5.2 发送能力

#### FR-01 API 发送
- `POST /api/v1/messages/send`，X-Api-Key
- 入参：category（必填）、mobiles（≤10000）、content 或 template_id+template_params、sign_name、scheduled_at、biz_id（幂等键）
- **模板变量（v1.2）**：模板含 `{1}..{n}` 占位；调用方传 `template_params` 字符串数组（本期全批次同参）；**平台完成渲染**后下发，并校验参数个数与渲染结果长度（≤500 字），从源头规避厂商 10002 模板不匹配
- 幂等：同应用同 biz_id 24h 内返回原批次，`idempotent: true`；Redis 键 `idem:{app_id}:{biz_id}` TTL=86400，DB `idempotency_record` 保存唯一键与 expires_at，过期记录清理后允许 biz_id 再次使用
- 免审批；受类别策略、配额（计费条）、限流、频控约束

#### FR-02 Web 人工发送
- 号码来源：手工粘贴 或 import_task 引用（上传 xlsx/csv：**单文件≤10MB、≤5万号码**（可配），openpyxl 只读流式解析防炸弹；有效号码逐条落 import_phone 的 enc/hmac/mask/key_version；预检出有效/无效/重复/黑名单四类；剔除清单仅含掩码号码、原行号与原因，24h 有效）
- 类别选择（notice/market）、模板渲染预览、定时、**计费条与配额消耗预览**
- **market 强制勾选"已获用户同意发送营销信息"**，勾选行为与操作人记入审计
- **测试发送（v1.2）**：勾选后仅允许 ≤5 个号码，豁免营销时间窗与审批（阈值内本就免审），黑名单/敏感词/频控/配额照常执行；批次标记 is_test，报表可过滤

#### FR-03 定时发送
- 平台自行调度；scheduled 批次 beat 每分钟扫描投递；到点前可取消/改期（改期重审）；营销窗外自动转定时（is_test 豁免）

#### FR-04 发送审批
- Web 渠道按类别阈值触发；审批申请人与审批人同时固化不可变 `account_id`/`identity_id`，回避判断只比较 `account_id`（不能审本人提交，接口 403 + DB CHECK），登录名只作事件时展示；历史申请主体无法证明时禁止审批。通过入队/驳回回补配额（计费条）；24h 过期自动关闭并通知；动作记审计 + 企业微信通知

#### FR-05 批次分片、厂商调用与可靠性
- 按 vendor_batch_size（默认500）分片；全局令牌桶（realtime 预留）
- 错误码：5002/5003/429 退避（≤5次）；999/99 → balance_blocked 双队列暂停 + 告警，**管理员"队列恢复"一键操作（v1.2 补接口）**；1006 折半重试一次；1011 延迟30min；参数类不重试
- **Send 超时 = 结果未知**：chunk 置 uncertain，禁自动重发；reconcile 在 raw_vendor_log 按 customId 比对修复，24h 未决转人工告警
- 对账任务每 5min 兜底重投（submitted/uncertain 不重投）；Redis 故障可恢复

#### FR-05a 失败重发
- 批次详情"失败号码一键重发"：新批次（resend_of 溯源），完整走管控流水线（含频控/时间窗/审批）

### 5.3 状态报告、回复与结果回调

#### FR-06 状态报告轮询与原始报文落地
- GetReport 每 60s；**先把完整响应 AES-GCM 加密落 raw_vendor_log.payload_enc，并保存不含手机号的 custom_ids 索引元数据，提交后再受控解密解析**（拉走即消费兜底）；幂等回写，48h 超时置 unknown；批次终态触发回调；raw 密文保留 90 天可重放
- **unmatched 报告（v1.4）**：解析时 customId 匹配不到平台分片的记录**不丢弃**，落 unmatched_report 表（号码三列加密），运维中心可查可导出——并行迁移期承接旧系统直连产生的报告，供其人工对账；保留 90 天

#### FR-07 上行回复采集
- GetReply 每 5min，同样先落地；回复查询、退订关键字一键加黑

#### FR-07a 结果回调
- 应用配置 callback_url（内网 CIDR 白名单，防 SSRF）+ callback_secret（**支持轮换端点，v1.2**）
- 事件：batch.finished（终态汇总）；message.report（应用开关，分钟聚合 ≤500 条）。callback_task 只保存批次/消息引用和无 PII 元数据，worker 投递时临时解密手机号构造 body，任务表与日志不得保存明文 body
- 生产 callback URL 只允许 HTTPS，可选加载受控 CA 与 mTLS 客户端证书/私钥文件；HTTP 只允许显式 development/test Mock。出站前重新解析 DNS 并固定已批准 IP，同时保留原 Host/SNI，禁止重定向；固定 IP 请求禁用 keep-alive，避免共享 IP 上跨逻辑主机复用旧 TLS 会话；连接并发、单地址连接预算、总超时、响应头与正文均有硬上限。应用停用、关闭明细回调、修改 callback URL 或轮换 callback secret 时，必须在同一事务把尚未终结的旧回调隔离为不可重试；投递与人工重试还必须复验当前应用状态、URL、Secret 密文和明细开关完全匹配，配置撤销后不得继续解密或投递 PII
- 签名：X-Sms-Timestamp + X-Sms-Signature = HMAC-SHA256(secret, timestamp+"."+body)，时间戳偏差 ≤300s；event_id、callback secret 密文/密钥版本和签名协议版本在 callback_task 创建时固化，但固化值仅在当前应用配置仍完全匹配时有效
- 重试 60/300/900/3600/3600s 共5次 → dead 告警，可手动重推；独立 callback worker

### 5.4 内容与号码管控

#### FR-08 去重：批内自动去重，计入统计
#### FR-09 黑名单：HMAC 存储；按类别策略生效；单个/批量/退订加黑
#### FR-10 敏感词：Aho-Corasick；block（默认）/audit 策略
#### FR-11 配额与限流
- 配额（**计费条**）：应用日配额、部门日配额（0=不限）；PostgreSQL 事实账本对稳定请求串行预留，批次同事务提交，驳回/过期/取消/全量剔除/入库失败/幂等复用经 Outbox 请求唯一释放
- Redis 仅是带版本的绝对计数投影，必须可从 PostgreSQL 重建；投影缺失、重建中或 Redis 不可确认时返回 503 `USAGE_PROJECTION_UNAVAILABLE`，禁止把缺失当零
- 限流：应用每分钟受理次数（默认60）；全局厂商 QPS
#### FR-11a 号码级频控（v1.2 新增）
- 规则见 FR-00 矩阵；PostgreSQL 只为 accepted/committed 的号码主体记账，Redis 投影键保持 verify 全局、market 应用维度，窗口过期对齐分钟/上海自然日
- 手机号仍只以 HMAC 进入事实；保留版本的 HMAC alias 必须归并为同一不可逆主体，防止轮换后绕过频控
- 应用级覆盖：app.freq_override（JSONB），管理员配置
- 频控剔除不回补已发生的计数；受理响应与批次统计含 removed_freq_limit

### 5.5 模板与签名管理

#### FR-12 模板：CRUD + `{n}` 占位规范（每变量登记最大长度 var_specs）+ BindTemplate/GetTemplateState 同步；**提交厂商时按序转换为厂商 `{s最大长度}` 格式（v1.3）**；Bind 意图必须与模板变更同事务进入 PostgreSQL Outbox，由持有厂商凭据的 realtime worker 执行，API 不挂载厂商凭据且不直接出站；仅厂商通过可用；渲染时校验参数个数与各参数长度 ≤ max_len，见 FR-01 与 docs/vendor-api.md 1.5 节
#### FR-13 签名：CRUD + BindSign/GetSignState；Bind 意图同样经事务性 Outbox 交给 realtime worker，API 仅返回 pending；应用默认签名；自动拼接与一致性校验

### 5.6 告警与监控

#### FR-14 余额巡检：每 10min 快照；低于阈值（默认10000 计费条）告警（4h 去重）；99/999 即时告警
#### FR-15 异常告警：失败率超阈、厂商连续失败、鉴权/IP 校验错误、uncertain 超 24h、回调 dead；企业微信 + SMTP

#### FR-25 发送量异常检测（v1.5 新增）
- beat 每 anomaly_scan_minutes（默认60）按 **应用×类别** 比对：当日累计量 vs 近 7 日同时段基线均值
- 触发（两者同时满足才告警，避免小基数误报）：当日量 > 基线 × anomaly_multiplier（默认3）**且** 当日量 ≥ anomaly_min_total（默认500）
- verify 类升级为 **crit**（密钥盗刷/验证码轰炸嫌疑），告警文案附"建议：核查该应用调用来源，必要时停用 API Key 或轮换"
- 新接入应用前 7 日无基线：仅按 anomaly_min_total 的 5 倍做绝对值兜底
- 去重键 anomaly:{app_id}:{category}:{date}，同日同源只告一次；开关 anomaly_enabled（默认开）
- 数据来源：当日 PostgreSQL 用量事实投影 + stat_daily 基线，Redis 仅用于读取加速，零新增 PII 采集

#### FR-27 后台任务健康（v1.5 新增）
- 所有 beat 任务经 @tracked_job 包装，落 job_run 表：任务名、起止、耗时、处理条数、结果、错误摘要；保留 job_history_days（默认30）天
- 告警两类：**连续失败 ≥ 3 次**（job_failed）；**心跳缺失**——超过预期间隔 × 2 未见新记录（job_stalled，捕获 beat 静默挂掉）
- 心跳巡检自身由 api 进程内轻量定时器执行（不依赖 beat，否则 beat 挂了没人报）
- 仪表盘"任务健康"格（N 任务绿/红点+最近运行）；运维中心新增"任务健康"tab（列表：任务/上次运行/耗时/处理量/近24h成功率/手动触发）

### 5.7 报表与查询

#### FR-16 查询（v1.2 补齐 Web 端点）
- 批次列表 `GET /web/batches`：时间/类别/状态/渠道/应用/部门/是否测试 多条件
- **跨批次号码搜索** `GET /web/messages`：按手机号（HMAC 精确）+ 时间范围检索该号码全部收发记录（排障常用）
- 批次详情与明细：列表 phone_mask，详情按角色解密（记审计）
- 数据权限：operator/viewer 本部门；导出 CSV ≤10万行异步，decrypted 需高权限并审计；所有导出文件均 AES-GCM 密文落盘，下载端鉴权后流式解密，磁盘与临时目录不得出现明文手机号
- 导出任务对象授权：外部仅使用不可枚举 UUID；admin 可访问已解析任务，approver 仅本人或同部门，operator/viewer 仅本人掩码任务。状态与下载执行同一授权；创建人同时固化 `account_id`/`identity_id`，历史用户名不得反查猜测主体，无法证明时对所有角色 fail closed。明文下载需绑定稳定账号、当前 JWT 会话、IP 与具体任务的五分钟单次二次认证令牌，并写无手机号的事务审计

#### FR-26 号码时间线（v1.5 新增）
- 端点 GET /web/messages/timeline：输入单个手机号（HMAC 精确）+ 时间范围，返回该号码**下行消息与上行回复合并按时间排序**的事件流；每条含类别、内容摘要（verify 已打码）、状态、关联批次、提交方
- 页面：号码搜索页提供"列表 / 时间线"两种视图；时间线按日分组，下行左侧、上行回复右侧缩进并标"↩ 用户回复"
- 场景：12321 投诉核查、客服排障——一屏还原"我们何时发了什么、用户回了什么、是否已退订加黑"
- 权限与脱敏同 FR-16；页顶展示该号码状态徽标（是否黑名单/退订来源/近30日接收量）

#### FR-17 统计报表
- **成功率口径（v1.4，全端统一）**：`成功率 = delivered / (delivered + failed)`；unknown/other 不进分母，单列展示。实现集中于 services/stats.py
- 日/周/月 × 应用/部门 × 类别：消息数、**计费条数**、成功率；stat_daily 聚合
- 仪表盘：今日分类别消息数与计费条、成功率、余额曲线、待审批、告警、uncertain 数、unmatched 数（v1.4）

### 5.8 系统管理

#### FR-18 应用与密钥
- CRUD：allowed_categories、默认签名、日配额（计费条）、限流、blacklist_check、freq_override、回调配置
- API Key：一次性明文；**双 Key 轮换**（宽限期默认72h，可提前作废）；callback_secret 轮换
- 来源 IP 白名单（v1.6.43）：应用级 `allowed_ips` 配置规范化 CIDR（单 IP 自动归一化为 /32 或 /128，上限 50 条，空=不限）；仅作用于 X-Api-Key 认证路径（发送、批次查询、uat-send），修改立即生效；来源不在白名单返回 `403 IP_NOT_ALLOWED`，Web 用户 JWT 路径不受影响
#### FR-18a 用户管理（v1.2 补）
- 统一账号台账显示认证源、账号/身份状态、本地凭据状态、AD 同步状态、当前角色与来源组；全部写操作以稳定 `account_id` 定位
- 本地账号仅由管理员创建，设置临时密码；支持启停、重置临时密码、角色调整和强制下线，不提供注册、重命名、硬删除
- AD 账号首次成功登录后进入台账；支持角色人工覆盖（role_override）与恢复跟随 AD 组映射；AD 密码不得进入用户管理 API 或页面
- 用户名与密码规则必须常驻显示；停用账号和密码重置须显式二次确认；保护当前管理员与最后一个有效管理员
#### FR-19 系统参数与认证源
- sys_config：v1.1 全量 + v1.2 新增（退订语、频控、导入边界、测试发送上限）；改动记审计
- 认证源：本地 Provider 固定启用且不可修改；AD 在系统配置页按“保存草稿 → 测试连接 → 启用配置”生效，编辑草稿立即使旧测试资格失效；“禁用 AD”只隐藏登录入口并保留生效配置与角色映射
- 页面只显示 LDAP bind secret 与 CA 文件是否就绪，不接收、不回显凭据或文件内容
#### FR-19a 真实联调控制台与受控 API UAT（v1.6.3）
- 单机开发测试环境的正式运营商生命周期控制全部在 admin 的系统配置页完成：状态、正式 Key 安装/轮换、加密测试号码、激活/暂停/恢复、页面单号码 UAT 与结果查看
- 正式 Key 明文只可在页面组件的浏览器易失内存中短暂存在，由 WebCrypto 封装后普通 API 只转发密文；root `vendor-control-agent` 经固定 Unix Socket 解密并原子安装版本化凭据，API/worker 不得获得 root、sudo 或 Docker Socket
- 安装/轮换 Key、激活和 critical resume 必须按当前 local/AD Provider 二次认证；5 分钟单用途令牌只允许消费一次，不允许 Provider 回退
- 测试号码按 `phone_enc/phone_hmac/phone_mask/key_version` 存 PostgreSQL；页面只展示备注与掩码。live-test 模式下普通发送入口返回 `VENDOR_TEST_CONSOLE_ONLY`，控制台 UAT 每次只选择一个 active recipient
- 应用侧唯一真实联调例外为 `POST /api/v1/messages/uat-send`：有效 `X-Api-Key`、仅通知、单个 active 已登记号码、直接内容或已审核模板与必填 1–32 位 `biz_id`；禁止定时、验证码、营销及额外字段。API 必须先消费应用限流并校验通知权限，再以全版本 HMAC 定位号码且要求所有保留版本 digest 与当次输入完全匹配，只把既有加密四元组交给同一 pipeline；worker 加载分片时与号码维护共享同一 advisory xact lock 并再次验证 active，queued/sending 测试批次作为维护租约，终态前禁止停用或删除。控制状态损坏或过期必须以 Redis 原子命令同时写入两个独立 agent-stale critical pause 键，写入未确认则保持 503 且不继续；复用 24h 幂等、每日 100 个计费条和 uncertain 占额
- 继续执行上海自然日 100 个计费条硬上限、uncertain 持续占额、1010/鉴权/余额等 critical pause 和 GetBalance-only 激活预检；CI/G2 始终使用 Mock/FakeCarrier
#### FR-19b 安全日报配置与投递（v1.6.40）
- admin 的 `/security-daily` 页面提供启停、Resend Key 和最多 3 个收件人配置；Key 留空表示保持原值，页面只显示“已配置/未配置”，不回显 Key
- 配置写入 `sys_config.security_daily_resend_api_key` 与 `security_daily_recipient`，配置变更写审计但审计只包含 configured 状态、启停状态和收件人数；配置保存后由 API 原子同步 `resend.json` 给独立 mailer
- 独立 mailer 仍固定使用 `reports.neuer.cn` 发件域名和 `api.resend.com`，平台 API/worker/beat 不直接访问 Resend；日报正文只允许已脱敏结构化 payload，发送、重试、预览和 `unavailable` 语义保持不变
- 主机侧 `security-report-collector` 在 08:00 前把前一上海自然日的日志聚合为脱敏结构化快照；缺少全部证据源时必须保持 `generation_status=unavailable`，不得用示例或零值替代。部分来源缺失但仍有真实来源时可生成 `attention` 报告，并在 `coverage` 明确列出缺口。
#### FR-20 审计日志
- 覆盖全部写操作与敏感读（解密查看/导出/报文重放/回调重推/队列恢复/角色覆盖）
- **防 PII（v1.2）**：审计记录只存号码数量与批次引用，**禁止存手机号列表明文**
- **稳定主体（v1.6.2）**：人类事件写 `actor_account_id` 与当次 `actor_identity_id`，API 应用事件写 `actor_app_id`；`actor`/角色/部门均为事件时展示快照。审计查询支持按 `account_id` 串联改名前后事件；无法证明的历史事件显式为 `legacy_unknown`，禁止按当前用户名猜绑定。
- 审批、导出、导入、真实联调 operation 与 Web 批次的所有权和职责分离只使用不可变主体 ID；新账号复用旧登录名不得继承旧资源。
- 只增不改（DB 权限回收）；仅 admin 可查

### 5.9 数据安全与生命周期

#### FR-22 手机号字段级加密：逐号码持久化使用 enc/hmac/mask + key_version；应用层 AES-256-GCM；密钥 Docker secrets；文件/JSONB/Redis/日志同样禁明文。对外解密仅详情/授权导出；内部 raw 解析/重放与 callback 投递只允许内存中受控解密且不得再次持久化。新密文使用版本化 envelope/AAD，至少绑定 domain、table、column、不可变 object ID 与 key_version；phone、短信正文、callback secret、raw payload、export frame 使用不同 domain。迁移期允许受控双读旧通用 AAD，后台逐批重加密且不持久化中间明文，确认旧格式计数归零后才可关闭兼容读取
#### FR-22a verify 内容 OTP 打码（v1.4 新增）
- 开关 sys_config.verify_otp_mask（默认开）：verify 类批次 content **入库前**将其中 4–8 位连续数字做**等长星号替换**（`验证码是541254` → `验证码是******`），OTP 明文不落库、不进日志、不进回调
- 等长替换保证长度不变 → 计费条计算不受影响（计费仍在打码前的渲染内容上进行，两者结果一致）
- 仅影响存储与展示；实际下发厂商的是打码前原文
#### FR-21 生命周期：消息/回复 1 年（月分区）、审计 3 年（仅 sms_owner 按 DBA 变更单先加密归档校验再清理，应用代码无删除权）、raw 密文 90 天、import/import_phone 24h、导出密文 7 天、批次汇总与报表长期

## 6. 非功能需求

| 编号 | 要求 |
|---|---|
| NFR-01 | 性能：日均 1万~10万条；API 受理 P95<300ms（万级批次<3s，含加密与计费计算）；verify 端到端 P95<2s |
| NFR-02 | 可用性（v1.2 冷备）：主节点 Compose 单机；**冷备节点**预装同版本栈，每日同步（pg_dump 全量 + deploy 配置 + secrets 手动首次分发），**RTO ≤ 30min、RPO ≤ 24h**；deploy/failover.md 提供切换手册（含 DNS/hosts 切换、数据恢复、厂商出口 IP 双报备）；**每季度切换演练一次**；生产 Redis broker/auth/control 使用三个不同托管高可用端点，Outbox 与 PostgreSQL 用量事实分别提供可恢复边界 |
| NFR-03 | 安全：生产运行凭据与三个 Redis ACL 密码仅 Docker secrets；DB owner 与 auth/accept/send/callback/export/scheduler/metrics 七职责分离，运行角色非 owner/超级用户、无 DDL 且 audit 不可修改/删除；API Key 哈希+双Key；JWT jti 吊销+强制下线；HTTPS；登录锁定+IP限流；回调 HMAC+CIDR 白名单 |
| NFR-04 | 合规：PIPL 加密/脱敏/解密审计；等保三级审计只增3年；**营销合规：退订语强制、用户同意留痕、退订即时加黑（12321 投诉可举证）** |
| NFR-05 | 可观测：JSON 日志（手机号强制掩码）、/healthz、Prometheus/运维指标（双队列/速率/厂商错误/uncertain/回调失败/频控剔除数/Outbox积压、失败次数、最老事件年龄、dead-letter、用量投影漂移维度数与绝对差）；漂移日志、告警与解释输出不得包含手机号或 HMAC |
| NFR-06 | 兼容：Chrome/Edge 最新版；Python 3.12、PostgreSQL 16、Redis 7 |
| NFR-07 | 运行时边界：必须显式声明 `ENVIRONMENT=development|test|production`，模式与 DEBUG/Mock 组合不一致即启动失败；生产关闭 Swagger/ReDoc/OpenAPI；API client 与 Web user 路由必须声明认证 dependency 并由契约测试覆盖；QPS、导入大小/行数、超时与锁定时间必须有上界及跨字段校验 |

## 7. 厂商接口对接约定（摘要）

| 平台功能 | 厂商接口 | 关键约束 |
|---|---|---|
| 发送 | POST /Sms/Api/Send | 内容≤500字（渲染后校验）；CustomId 32位=批次前缀24+分片序号8；返回 taskId |
| 报告/回复 | GetReport / GetReply | **拉走即消费**→完整响应先加密落 raw_vendor_log，再受控解密解析 |
| 余额 | GetBalance | 定时巡检（单位：计费条） |
| 模板/签名 | Bind*/Get*State | 厂商人工审核；平台侧渲染校验前置 |
| 频控 | 5002/5003/429 | 退避+令牌桶（realtime 预留） |
| 余额不足 | 99/999 | 双队列暂停+一键恢复 |
| 超时 | — | uncertain，禁自动重发 |

完整错误码映射见 `docs/vendor-api.md` 第 4 节（唯一依据）。

## 8. 里程碑

| 阶段 | 内容 | 工期建议 |
|---|---|---|
| M1 | 骨架：加密、计费条、认证、应用管理、适配层+模拟器、API发送（幂等/分类/频控/模板渲染）、报告落地轮询、uncertain | 3 周 |
| M2 | 管控：黑名单/敏感词、配额限流（计费条）、审批回避、时间窗、营销合规、测试发送、Web发送导入、对账 | 2 周 |
| M3 | 运营：模板/签名、余额告警、回复、结果回调（含密钥轮换） | 2 周 |
| M4 | 报表审计交付：查询/号码搜索/导出、统计（计费条）、用户管理、审计、生命周期、冷备与切换演练、验收 | 2 周 |

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| GetReport 拉走即消费 | raw_vendor_log 先落地可重放 |
| Send 超时重复下发 | uncertain + 比对修复 + 告警兜底 |
| 计费条口径与厂商账单不一致 | billing.py 单点实现；上线首月与厂商账单逐日核对，偏差>1%排查规则 |
| 频控误伤正常业务（如同号多次合法验证码） | 参数可配+应用级覆盖；剔除明细可查 |
| 冷备数据陈旧（RPO 24h） | 切换手册含"从 raw_vendor_log 与厂商侧补对账"步骤；关键日可手动加频备份 |
| 厂商频控/号码上限未量化 | QPS/分片可配；出口 IP 主备双报备（规避 1010） |
| 营销窗外转定时不符预期 | 响应明确 deferred + 可取消；测试发送豁免 |
| 回调打爆内网 | CIDR 白名单+超时+退避+dead 熔断 |

## 10. 上线切换与并行期方案（v1.4 新增）

### 10.1 核心约束
厂商 GetReport/GetReply 为**拉走即消费**：同一密钥下任意一方拉取，另一方即永久取不到。并行期若旧系统与平台同时轮询，双方报告互相"偷走"。

### 10.2 切换路线（一刀切拉取权）
1. **T0（平台上线日）前**：行政通知全部直连系统——T0 起**禁止调用 GetReport/GetReply**（Send 可暂时保留直连）；有条件的在网络层对旧系统源 IP 封禁厂商域名兜底
2. **T0 起**：报告/回复拉取权唯一归平台；平台匹配不到自家 customId 的报告落 unmatched_report（FR-06），旧系统需要下发结果时到平台运维中心查询/导出对账
3. **迁移顺序**：notice 类低风险应用 → verify 类（IAM，切换窗口选业务低峰，切前压测通过）→ market 类；每应用迁移 = 领取 API Key + 改调平台 API + 验证回调/查询 + 观察 3 天
4. **T0+N 收口**：全部应用迁移完成后，请厂商重置密钥（旧直连彻底失效），平台更新 secrets

### 10.3 回退预案
- **单应用回退**：平台停用其 API Key，该应用恢复直连 Send（密钥未重置前可行）；报告仍由平台代查（unmatched 可按号码/时间导出）
- **平台整体回退**：停平台轮询与发送 worker → 通知各系统恢复直连 → 平台数据只读保留；回退决策人：安全负责人
- 收口重置密钥后不再支持直连回退，需按重大变更流程审批后方可执行收口

### 10.4 切换检查清单
- [ ] 全量直连系统清单与负责人确认签收"停拉通知"
- [ ] 平台 uncertain/unmatched 监控就绪，值班表排定（T0 起 3 天加强值守）
- [ ] 厂商侧确认：当前仅一个密钥、QPS 与单次号码上限书面确认、出口 IP 主备已报备
- [ ] 冷备当日全量同步一次并演练恢复
- [ ] 回退演练：任选一测试应用走一遍停 Key→直连→再迁回

## 附录A：前端页面清单（v1.2，Claude Code 建站基准）

| # | 路由 | 页面 | 关键功能 | 可见角色 |
|---|---|---|---|---|
| 1 | /login | 登录 | 显式选择本地/AD 认证源且不回退、锁定/封禁提示、首次改密跳转 | 全部 |
| 1a | /change-password | 首次改密 | 仅接受短期 change token；展示密码规则，成功后返回登录页 | 临时本地账号 |
| 2 | /dashboard | 仪表盘 | 分类别今日量与计费条、成功率、余额曲线、待审批、告警、uncertain、**任务健康格（v1.5）** | 全部 |
| 3 | /send | 人工发送 | 类别选择、粘贴/导入、模板渲染预览、计费条与配额预览、定时、测试发送、market 同意勾选 | operator+ |
| 4 | /batches | 批次列表 | 多条件过滤（含 is_test）、状态跟踪 | viewer+（本部门） |
| 5 | /batches/:no | 批次详情 | 统计、明细（mask）、失败重发、取消、详情解密（按角色） | viewer+（本部门） |
| 6 | /messages | 号码搜索 | 跨批次检索；**列表/时间线双视图，下行+回复合并事件流，号码状态徽标（v1.5）** | viewer+ |
| 7 | /approvals | 审批中心 | 待办/已办、通过/驳回（本人单隐藏操作） | approver, admin |
| 8 | /templates | 模板管理 | CRUD、{n} 占位、厂商状态同步 | operator+ |
| 9 | /signs | 签名管理 | 列表（operator 可见）、申请与同步（admin） | operator+/admin |
| 10 | /replies | 回复查询 | 列表、退订一键加黑 | viewer+ |
| 11 | /blacklist | 黑名单 | 列表（mask）、添加/导入 | admin |
| 12 | /sensitive-words | 敏感词 | 列表、批量导入、策略切换 | admin |
| 13 | /apps | 应用管理 | CRUD、Key 轮换/作废、回调配置与密钥轮换、频控覆盖 | admin |
| 14 | /users | 用户与角色 | 本地账号创建/重置/启停、Provider/凭据/同步状态、AD 角色跟随或覆盖、强制下线 | admin |
| 15 | /configs | 系统配置 | 页签：运行参数、认证源、真实联调；含 AD 草稿/测试/启停、sys_config 分组编辑及受控正式 Key、测试号码、激活和单号码 UAT | admin |
| 15a | /security-daily | 安全日报 | 查看生成/投递状态、配置 Resend Key 与收件人、预览、手动投递与失败重试 | admin |
| 16 | /audit | 审计日志 | 多条件检索（只读） | admin |
| 17 | /reports | 统计报表 | 日/周/月 × 应用/部门 × 类别，消息数+计费条，导出 | viewer+ |
| 18 | /ops | 运维中心 | Tab：告警记录 / 回调任务（重推）/ 原始报文（重放）/ uncertain 分片 / unmatched 报告 / **任务健康（v1.5）** / 队列恢复 | admin |
