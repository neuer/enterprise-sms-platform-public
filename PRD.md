# 企业短信管理平台 PRD

| 项 | 内容 |
|---|---|
| 文档版本 | v1.6.45 |
| 日期 | 2026-08-28 |
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
| v1.4 | ①**上线切换与并行期方案**（第10章：原“一刀切拉取权”已由 Phase 0 修订为逐应用切换、不同服务商各自唯一轮询器+unmatched 保留+回退预案） ②**verify 类 OTP 等长打码**（默认开，OTP 不落库） ③成功率口径统一（unknown 不入分母） ④ops 支撑端点补齐（告警/raw列表/uncertain列表/unmatched） ⑤docs/UAT.md 验收用例集 ⑥M4 增压测任务 ⑦契约回填与认证方式规则 |
| v1.5 | **三个新功能（不改既有设计）**：①FR-25 发送量异常检测（基线×3σ 突增告警，密钥盗刷/接入方 bug 兜底） ②FR-26 号码时间线（单号码收发全history，投诉核查一屏还原） ③FR-27 后台任务健康（job_run 记录+心跳缺失告警+看板，消除 beat 静默挂掉盲区）；docs/ 收编 UI 设计稿两件套 |
| v1.6 | **无人值守执行层与规格闭环**：AUTOPILOT.md（DONE 定义/分层门禁/3次失败 BLOCKED/跨会话续跑）、AUTH_MOCK、告警 log-sink、消歧规则 31–37、owner/app 双角色、敏感中间产物加密、可过期 DB 幂等、完整 OpenAPI/Mock/UAT、[HANDOVER] 人工移交标记 |
| v1.6.1 | **P0 账号体系重构**：内置本地账号与 AD Provider 并存、登录显式选择认证源、首个本地管理员独立初始化、首次强制改密、稳定主体/身份/凭据分层模型、AD 草稿测试激活工作流；未来 IAM Provider 仅预留扩展能力，本期不实现 |
| v1.6.4 | **事务性可靠发布**：业务状态与无 PII `outbox_event` 在同一 PostgreSQL 事务提交；独立 dispatcher 以租约/fencing 发布，Celery task ID 固定为 event ID，消费者按事件 ID 幂等；broker 首次失败不改变 HTTP 已受理语义，恢复扫描仅作兜底 |
| v1.6.5 | **可恢复用量事实**：配额和号码频控改由 PostgreSQL 事实账本串行判定，Redis 仅保存版本化绝对投影；终态释放经事务性 Outbox 幂等补偿，支持 HMAC 轮换归并、漂移告警、无 PII 解释和安全重建 |
| v1.6.6 | **Redis 故障域硬化**：Celery broker、认证会话、业务控制面拆为三个独立高可用端点和 ACL 身份；密码只经 Docker secrets，认证故障 fail closed，broker/control 分别由 Outbox 与 PostgreSQL 事实恢复 |
| v1.6.40 | **安全日报配置 UI 化**：管理员页面配置 Resend Key、收件人和启停状态，API 同步独立 mailer 配置文件；不再要求手工维护 Docker secret 与收件人文件 |
| v1.6.41 | **会话边界契约回填**：与已落地实现及 D048 修订对齐——access JWT 仅内存 + Bearer；refresh JWT 改 HttpOnly Cookie 并要求 refresh/logout 同源校验；高风险短期令牌仍仅组件局部易失内存 |
| v1.6.42 | **生产 Phase 0 决策基线**：生产与测试空库、数据和 secrets 完全隔离；首发经 VPN 接入，旧短信系统（不同服务商）长期并行且可切回；短信发送能力的业务 RTO≤12h、平台数据 RPO≤24h，冷备与备出口暂非首发硬门禁；Core、PostgreSQL 与 broker/auth/control 三个 isolated-standalone Redis 实例合并部署在单台生产 VM，明确接受整机共同故障域；首批仅 1–2 个低风险 notice 应用并观察至少 3 天；统一保留期与企微+公司邮件告警边界。业务 RTO 从最早业务不可用或受控关闭新入口的 `outage_start` 计起，可由新平台恢复或旧系统受控切回并完成最小验收结束；新平台恢复耗时另记。该条只记录决策，不证明生产基础设施、运行配置或发布证据已经满足。 |
| v1.6.43 | **复用厂商已有签名**：管理员可为本地待审核签名提交厂商已有正整数签名 ID，并再次确认本地签名名称；API 只把精确关联意图排入 realtime worker，worker 仅调用 GetSignState 复核并回写，不重复 BindSign、不发送短信；202 仅代表受理排队。 |
| v1.6.44 | **模板与签名审核状态持续跟踪**：每 10 分钟同步所有已有厂商编号的模板和签名，允许厂商状态在 pending/approved/rejected 间双向变化；手动同步同样覆盖三态。平台要求批量响应与请求 ID 一一对应，拒绝缺失、额外、重复或非法项；厂商编号在本地保持一对一；仅 rejected 保留打码且限长的驳回原因，其他状态清空旧原因；写回使用行版本与厂商编号 CAS，迟到结果不得覆盖新事实。模板绑定厂商编号后保持不可变，被拒绝或撤销时新建，避免在途发送把旧内容与新编号错配。生产非 Mock 发送还必须同时满足 approved 与合法正整数厂商编号；签名关联已有 ID 会原子撤销尚未发布的自动 BindSign，自动绑定一旦发布或执行则返回冲突，禁止双路并发。 |
| v1.6.45 | **模板改为全局资源**：平台模板不按用户、应用或部门隔离；所有应用均以全局唯一的平台模板 ID 引用已审核模板，厂商编号只用于平台对接厂商，不得作为发送接口的 `template_id`。模板的创建、送审、同步和不可变规则保持不变，应用仍受类别、配额、限流、频控、签名、IP 白名单与审批约束。历史 `sms_template.dept` 仅保留为兼容字段，不再参与查询、授权或发送判断。 |

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
建设统一的企业短信管理平台，作为已迁入应用访问新短信服务商凭据的**唯一通道**。Phase 0
期间旧系统使用另一服务商长期并行；目标态统一入口不授权一次性下线旧系统、共享凭据或
跨服务商混拉报告：

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
- 双活/K8s 高可用；Phase 0 为单主恢复方案，冷备建设保留但暂不作为首发硬门禁

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
| 操作员 | 本地维护 / AD 组映射或人工覆盖 | Web 人工发送（notice/market）、本部门记录、全局模板维护 |
| 只读用户 | 本地维护 / AD 组映射或人工覆盖 | 本部门记录与报表 |
| 接入应用 | API Key | 发送/查询 API，受 allowed_categories、配额、限流、频控约束 |

- Web 登录必须**显式选择认证源**并提交 `provider_code`；服务端只调用所选 Provider，**禁止自动回退**到其他认证源。所有 Provider 共享账号锁定（5次/15min）、IP 限流（5min 内失败≥20 封 15min）和统一错误话术。访问 JWT 固定 15 分钟，refresh JWT 最长 7 天且每次使用后由 Redis Lua 原子单次轮换；5 秒有界且不可延长的 grace 内，同一旧 token 的合法并发或丢响应重试不得吊销 family，超出窗口的重放仍吊销该 refresh family。成功登录若请求携带既有 refresh Cookie，须先吊销该 Cookie family 再签发新 family，避免在途旧 refresh 的 Set-Cookie 覆盖新会话；登录失败或仅返回首次改密令牌时不吊销。
- 每次访问 JWT 验证都必须读取数据库权威投影，并逐项匹配稳定 `account_id`、`identity_id`、Provider、登录名、部门、角色和统一 `security_version`，同时要求账号、身份及 Provider 均启用。账号/身份状态、部门、角色/覆盖、外部组映射、Provider 启停/生效配置、密码重置和强制下线的事务必须递增 `security_version`，使旧 access/refresh JWT 立即 401；数据库或 Redis 吊销/轮换状态不可用时返回 `AUTH_SESSION_UNAVAILABLE`/503 并 fail closed。
- AD refresh family 自最后一次完整密码认证起默认最多 480 分钟（可配置 15–10080 分钟）；多次 refresh 不得延长该起点。达到边界时必须原子吊销 family、删除 refresh Cookie 并返回 `AUTH_REAUTH_REQUIRED`/401，要求重新输入密码；local family 仍保持现有最长 7 天。策略读取或吊销状态不可确认时返回 503 并 fail closed。
- 平台使用**全局不区分大小写登录名空间**，用户名规范化后唯一，归属按**先到先得**确定。本地账号创建时不探测 AD；后续真实 AD 登录若与既有本地身份冲突，则拒绝并审计 `ACCOUNT_SOURCE_CONFLICT`，由管理员线下处理。
- 平台**不开放自助注册**，本地账号仅由管理员维护；管理员可创建、启停、重置临时密码、设置角色与强制下线，但不得重命名或硬删除账号。禁止停用自己，且不得停用或降级最后一个有效管理员。
- 本地密码为 12–128 位，大小写字母、数字、特殊字符至少三类，不能包含用户名，且新密码不得与当前密码或临时密码相同；管理员创建/重置后均标记为临时密码，用户**首次登录必须修改密码**。首次改密令牌只以哈希写入 PostgreSQL，并与主体、认证源、用途和签发时安全版本绑定；高成本校验前必须以短事务取得带 UUID fencing 的 `processing` 租约，只有有效租约可在同一最终事务完成令牌消费、密码更新、`must_change_password` 清除、`security_version` 递增及审计。已消费、过期、上下文失效或被未过期租约占用的令牌不得进入 Argon2；数据库结果不可确认时保持租约至到期并 fail closed。密码与用户名规则在登录、改密和用户管理界面提交前可见。
- Web 会话保持 Bearer Header 契约承载 **access JWT**：access 与用户快照仅保存在当前页面易失内存，禁止写入 Pinia 持久化或 Web Storage；历史 `sessionStorage`/`localStorage` 凭据只允许同步迁移一次并立即清除。**refresh JWT** 以受限路径的 HttpOnly Cookie 承载（生产必须 Secure，SameSite=Lax），且 refresh 请求体不得接受令牌；refresh/logout 必须校验规范化同源 Origin/Referer。注销、401、会话超时和强制下线后必须清理内存会话与 refresh Cookie，并用不含凭据的浏览器存储事件同步其他标签页。首次改密、导出 step-up 等高风险短期令牌只允许存在于发起操作的组件局部易失内存，不得进入 Pinia、浏览器存储、URL、日志或 DOM 持久节点；组件销毁或到期立即清空。生产入口的 HTTP 只能跳转到同一受控 HTTPS origin，最终响应必须满足 TLS 1.2+、HSTS、收紧后的 CSP 与证书到期监控门禁。
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

主节点 ──每日加密 pg_dump（保留 35 天）──▶ 受控备份目录
   └──后续冷备节点/备出口（Phase 0 暂非首发硬门禁，建成后执行恢复与切换演练）
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
- 幂等：同作用域同 biz_id 24h 内返回原批次，`idempotent: true`；Redis 键 `idem:{scope_kind}:{scope_id}:{biz_id}` TTL=86400，DB `idempotency_record` 以 `(scope_kind, scope_id, biz_id)` 为唯一键并保存 expires_at；`scope_kind='app'` 时 `scope_id` 绑定 `app_id`。过期记录清理后允许 biz_id 再次使用
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
- **unmatched 报告（v1.4）**：解析时 customId 匹配不到平台分片的记录**不丢弃**，落 unmatched_report 表（号码三列加密），运维中心可查可导出，用于核对新平台服务商凭据下的未知 customId、异常外部调用或恢复缺口；旧系统使用另一服务商，其报告不得进入本平台轮询或据此对账；保留 90 天

#### FR-07 上行回复采集
- GetReply 每 5min，同样先落地；回复查询、退订关键字一键加黑

#### FR-07a 结果回调
- 应用配置 callback_url（内网 CIDR 白名单，防 SSRF）+ callback_secret（**支持轮换端点，v1.2**）
- 事件：batch.finished（终态汇总）；message.report（应用开关，分钟聚合 ≤500 条）。callback_task 只保存批次/消息引用和无 PII 元数据，worker 投递时临时解密手机号构造 body，任务表与日志不得保存明文 body
- 生产 callback URL 只允许 HTTPS，可选加载受控 CA 与 mTLS 客户端证书/私钥文件；HTTP 只允许显式 development/test Mock。出站前重新解析 DNS 并固定已批准 IP，同时保留原 Host/SNI，禁止重定向；固定 IP 请求禁用 keep-alive，避免共享 IP 上跨逻辑主机复用旧 TLS 会话；连接并发、单地址连接预算、总超时、响应头与正文均有硬上限。应用停用、关闭明细回调、修改 callback URL 或轮换 callback secret 时，必须在同一事务把尚未终结的旧回调隔离为不可重试；投递与人工重试还必须复验当前应用状态、URL、Secret 密文和明细开关完全匹配，配置撤销后不得继续解密或投递 PII
- 签名：X-Sms-Timestamp + X-Sms-Signature = HMAC-SHA256(secret, timestamp+"."+body)，时间戳偏差 ≤300s；event_id、callback secret 密文/密钥版本和签名协议版本在 callback_task 创建时固化，但固化值仅在当前应用配置仍完全匹配时有效
- 失败回调（含 4xx，不只 5xx）按 60/300/900/3600/3600s 重试 5 次 → dead 告警，可手动重推；独立 callback worker

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

#### FR-12 模板：模板是全局资源，不按用户、应用或部门隔离；平台模板 ID 全局唯一，所有应用均可引用任一已审核模板，历史 `sms_template.dept` 仅作兼容且不得参与查询、授权或发送判断。模板支持 CRUD + `{n}` 占位规范（每变量登记最大长度 var_specs）+ BindTemplate/GetTemplateState 同步；**提交厂商时按序转换为厂商 `{s最大长度}` 格式（v1.3）**；Bind 意图必须与模板变更同事务进入 PostgreSQL Outbox，由持有厂商凭据的 realtime worker 执行，API 不挂载厂商凭据且不直接出站；仅厂商通过可用；生产非 Mock 发送还必须存在 1..2147483647 的规范十进制厂商模板编号，`approved + NULL/非法编号` 的异常或遗留记录不得使用；渲染时校验参数个数与各参数长度 ≤ max_len，见 FR-01 与 docs/vendor-api.md 1.5 节。所有已有厂商编号的 pending/approved/rejected 模板每 10 分钟持续同步并允许双向变更，手动同步覆盖同一范围；批量响应异常时整批不写回，非 rejected 状态不得保留驳回原因。模板一旦绑定厂商编号即不得原地编辑、重新绑定或删除；被拒绝或撤销后必须新建模板，避免并发受理中的旧内容与新编号错配。状态降级会阻止此后新请求使用；已经通过审核校验并受理的批次继续使用冻结正文与原厂商编号到达终态，不自动改写或重发
#### FR-13 签名：CRUD + BindSign/GetSignState；Bind 意图同样经事务性 Outbox 交给 realtime worker，API 仅返回 pending；应用默认签名；自动拼接与一致性校验。管理员可将本地待审核签名与厂商已有正整数签名 ID 精确关联，但必须再次提交与本地记录完全一致的签名名称；API 在同一事务锁定本地签名与活动 Outbox，允许原子撤销尚未发布的自动 BindSign 后排入关联意图；若自动绑定已经 leased/published/processing，或已有活动关联意图，则返回 409，禁止两条厂商路径并发。worker 仅调用 GetSignState 复核并回写本地状态，不调用 BindSign、不发送短信。HTTP 202 只表示排队成功，不表示关联或厂商审核状态同步完成。所有已有厂商编号的 pending/approved/rejected 签名每 10 分钟持续同步并允许双向变更；单行手动同步只查询当前签名，同一签名同一 UTC 分钟内合并请求，且不计作 beat 全量同步心跳；批量响应异常时整批不写回，非 rejected 状态不得保留驳回原因。生产非 Mock 发送必须同时满足 approved 与 1..2147483647 的规范十进制厂商编号；厂商编号在本地必须一对一绑定；已被应用或发送批次引用的签名禁止原地修改并重新绑定，应新建签名。状态降级会阻止此后新请求使用；已经通过审核校验并受理的批次继续使用冻结签名到达终态，不自动改写或重发

### 5.6 告警与监控

#### FR-14 余额巡检：每 10min 快照；低于阈值（默认10000 计费条）告警（4h 去重）；99/999 即时告警
#### FR-15 异常告警：失败率超阈、厂商连续失败、鉴权/IP 校验错误、uncertain 超 24h、回调 dead；生产通知渠道固定为企业微信 + 公司邮件。只有两个渠道的真实投递、接收人和升级路径均有 `[HANDOVER]` 证据才可称告警闭环，`log-sink` 或文档/Mock 测试不构成生产告警验收

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
- **跨批次号码搜索** `POST /web/messages`：手机号放请求体（HMAC 精确，禁止 query string），可加时间范围检索该号码全部收发记录（排障常用）
- 批次详情与明细：列表 phone_mask，详情按角色解密（记审计）
- 数据权限：operator/viewer 本部门；导出 CSV ≤10万行异步，decrypted 需高权限并审计；所有导出文件均 AES-GCM 密文落盘，下载端鉴权后流式解密，磁盘与临时目录不得出现明文手机号
- 导出任务对象授权：外部仅使用不可枚举 UUID；admin 可访问已解析任务，approver 仅本人或同部门，operator/viewer 仅本人掩码任务。状态与下载执行同一授权；创建人同时固化 `account_id`/`identity_id`，历史用户名不得反查猜测主体，无法证明时对所有角色 fail closed。明文下载需绑定稳定账号、当前 JWT 会话、IP 与具体任务的五分钟单次二次认证令牌，并写无手机号的事务审计

#### FR-26 号码时间线（v1.5 新增）
- 端点 `POST /web/messages/timeline`：手机号放请求体（HMAC 精确，禁止 query string），可加时间范围，返回该号码**下行消息与上行回复合并按时间排序**的事件流；每条含类别、内容摘要（verify 已打码）、状态、关联批次、提交方
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
- 已配置且无暂停的测试环境允许管理员二次认证后从 `controlled|inactive` 切回内置 Mock：先停止并确认测试侧真实发送与报告/回复消费者均不再运行，再删除测试环境正式厂商凭据并重建纯 Mock 服务；不等待已停止消费者无法自行收敛的测试 backlog，既有 queued/scheduled/pending/retrying 后续只允许命中 Mock，遗留 submitting 按既有恢复规则转 uncertain，uncertain 仍禁止自动重发；保留加密测试号码、短信业务数据、审计、当日 UAT/uncertain 事实、数据库与 volume；不得修改生产环境，也不得自动修复切换前历史未决状态
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
#### FR-21 生命周期：业务消息、回复及号码明细 12 个月（月分区）；审计 36 个月（仅 sms_owner 按 DBA 变更单先加密归档校验再清理，应用代码无删除权）；raw 密文与 unmatched 报告 90 天；import/import_phone 24h；导出密文 7 天；数据库加密备份 35 天；批次汇总与报表长期。策略文本不证明自动清理、归档或备份任务已在生产执行，必须以实际任务记录、对象年龄和隔离恢复证据验收

## 6. 非功能需求

| 编号 | 要求 |
|---|---|
| NFR-01 | 性能：日均 1万~10万条；API 受理 P95<2000ms（万级批次<3s，含加密与计费计算）；verify 端到端 P95<2s |
| NFR-02 | 可用性（Phase 0 单主 + 旧系统回退）：一台 12 vCPU/48 GiB 生产 VM 运行 Core、PostgreSQL 与三个 Redis 域的 Compose；**短信发送能力业务 RTO ≤ 12h、平台数据 RPO ≤ 24h**。`outage_start` 取最早的业务不可用证据或受控关闭新入口时刻，发现、决策、冻结和在途分类都计入；首批应用在新平台恢复，或完成“冻结上游与新平台发送面、稳定水位、分类 uncertain/submitted、路由互斥、切到旧系统并做最小发送验收”任一路径后，业务 RTO 才停止。旧系统切回不恢复新平台数据库，不能抵扣 RPO；新平台恢复耗时作为 `platform_recovery_elapsed` 另记。每日加密数据库备份保留 35 天，生产快照只在从预生产资源池按需创建的一次性、空白、隔离恢复机上验证；共享应用预生产不安装生产恢复材料、不承担生产快照恢复演练。冷备节点与备出口暂非首发硬门禁；其未建设意味着主节点、主出口或同园区故障时只能切回旧系统并另行恢复新平台，禁止宣称主备切换或机房级灾备已经具备。目标态仍是 broker/auth/control 三个独立高可用 Redis 故障域；Phase 0 已选同一生产 VM 内三个 `isolated-standalone` 实例，三者必须保持 host:port、数据目录、ACL 身份和密码隔离并强制 TLS 传输，但同时与 Core/PostgreSQL 共享 VM、宿主、维护窗口，Redis 三域共享 Redis VMDK 与 TLS 服务端私钥，是明确接受的共同故障域。生产必须显式设置 `REDIS_HA_MODE=isolated-standalone` 并由正式入口叠加 TLS/持久化合同；真实托管高可用才可设置 `managed`，禁止把单机伪报为 managed。精确最终 SHA 的配置校验、Compose 展开、25 件 secret、三域 ACL/TLS、业务回退演练和正式 release evidence 任一未通过即发布 No-Go。 |
| NFR-03 | 安全：生产运行凭据与三个 Redis ACL 密码仅 Docker secrets；DB owner 与 auth/accept/send/callback/export/scheduler/metrics 七职责分离，运行角色非 owner/超级用户、无 DDL 且 audit 不可修改/删除；API Key 哈希+双Key；JWT jti 吊销+强制下线；HTTPS；登录锁定+IP限流；回调 HMAC+CIDR 白名单 |
| NFR-04 | 合规：PIPL 加密/脱敏/解密审计；等保三级审计只增 36 个月；**营销合规：退订语强制、用户同意留痕、退订即时加黑（12321 投诉可举证）** |
| NFR-05 | 可观测：JSON 日志（手机号强制掩码）、`/livez`（进程存活）、`/readyz`（接流条件）、受 CIDR+Bearer 双重保护的 Prometheus/运维指标（双队列/速率/厂商错误/uncertain/回调失败/频控剔除数/Outbox积压、失败次数、最老事件年龄、dead-letter、用量投影漂移维度数与绝对差）；漂移日志、告警与解释输出不得包含手机号或 HMAC |
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
| 厂商额度只提供基础口径 | 当前口径为同一账户每秒最多 200 次调用、单次 Send 最多 100000 个号码；统计窗口、最小间隔、并发及接口合并范围仍未知。平台 QPS 配置硬上限 200，生产使用更低运行值并为非 Send 留余量；分片安全上限 1000、默认 500 |
| 营销窗外转定时不符预期 | 响应明确 deferred + 可取消；测试发送豁免 |
| 回调打爆内网 | CIDR 白名单+超时+退避+dead 熔断 |
| 单生产 VM 整体失效 | Core、PostgreSQL 与 broker/auth/control 同时不可用；三实例/端口/目录/ACL/secrets/TLS 只能降低横向越权，不能消除共同故障域。恢复依赖加密数据库备份，broker 依赖 Outbox 恢复、auth fail closed、control 写路径 503。`isolated-standalone` 的精确最终 SHA 与整机恢复证据未闭合前保持发布 No-Go |
| 无冷备、备出口和跨机房备份 | 首发明确接受停服恢复与旧系统切回；每日本地加密备份只能覆盖有限故障，不能宣称机房级灾备；后续建设不得用既有文档替代真实恢复/切换证据 |

## 10. 上线切换与并行期方案（v1.4 新增）

生产 Phase 0 的资源、制品、初始化、No-Go、证据分级和 `[HANDOVER]` 记录以
`docs/runbooks/production-phase0-baseline.md` 为执行基线；该文档不替代正式发布入口。

### 10.1 核心约束
旧短信系统与新平台使用**不同服务商、不同凭据和不同数据域**，计划长期并行；旧系统保留为可切回的发送通道。每一服务商的 GetReport/GetReply 仍是**拉走即消费**，同一组凭据只能由其所属系统的唯一轮询器消费；长期并行不授权共享凭据、复制生产数据或双轮询同一厂商。

生产从空库初始化。测试环境的数据库、手机号、消息、账号、API Key、厂商凭据、加密/HMAC/JWT 密钥、Redis ACL 密码、备份口令和运行目录均不得迁入或复用；仅允许经业务批准、确认无 secret/PII 的模板与非敏感配置通过受审计流程重新录入。

### 10.2 切换路线（逐应用切换与各服务商唯一拉取权）
1. **T0（新平台首发）前**：在预生产以与生产候选完全相同的镜像 digest 演练发布、恢复、认证、企微+公司邮件告警和单应用切回；生产仅允许经 VPN 到达受控入口，厂商出站使用主节点固定出口
2. **T0 首批**：只迁入 1–2 个低风险 `notice` 应用；每应用独立创建生产 API Key，执行最小真实发送、报告/回复、回调与查询验收，禁止搬用测试 Key 或测试数据
3. **调用方连续性合同**：每个首批应用必须证明至少 12 小时的持久请求积压、重启后不丢、24 小时内稳定复用 `biz_id`、新旧路由互斥，以及补发前人工审批；没有证据时不得把 RTO≤12h解释为请求不会丢或不会双发
4. **观察期**：每个首批应用连续观察至少 3 天，核对发送量、失败、uncertain、厂商账单、告警送达和回切可用性；任一关键证据失败即停止扩大范围
5. **长期并行**：旧系统继续服务未迁应用并保留切回能力；后续应用逐个审批迁移，不设置强制一次性收口或共享厂商密钥

### 10.3 回退预案
- **统一回退顺序**：记录 `outage_start`，先冻结上游新请求和自动重试、关闭新入口并停用对应 API Key；再围栏新平台发送 worker、Outbox dispatcher、beat 和厂商发送出站，取得稳定状态水位并分类 queued/submitting/submitted/uncertain。只有厂商和平台证据确认未受理的请求才可经双人批准补发；submitted/uncertain 禁止自动重发。随后才能把互斥路由切到旧系统及旧服务商并做最小发送验收
- **报告边界**：新平台是否继续 GetReport/GetReply 由其未终结批次决定，且始终只轮询新服务商；旧系统只轮询旧服务商。围栏发送面不授权关闭仍需的报告/对账链路
- 旧系统最小验收成功可停止“短信发送能力业务 RTO”，但不会把新平台数据倒灌旧系统、不会抵扣 RPO；新平台恢复继续按 `deploy/failover.md` 独立计时和记录

### 10.4 切换检查清单
- [ ] 生产与测试的空库、数据、25 件 canonical secrets（含三个 Redis ACL 密码和 `redis_tls_server_key`）、API Key 和运行目录隔离证据完成双人复核；新增 TLS 私钥只进入 `current/redis`，不得挂载到 backend
- [ ] 首批 1–2 个低风险 notice 应用、负责人、旧系统切回步骤和至少 3 天观察窗经审批
- [ ] 每个首批应用的 12 小时持久积压、稳定 `biz_id`、新旧路由互斥和补发审批合同已完成故障演练
- [ ] 平台 uncertain/unmatched 监控就绪；企业微信与公司邮件真实告警均已送达主接收人和替补
- [ ] 厂商侧确认：生产凭据、QPS、单次号码上限和主节点固定出口 IP；备出口暂非首发硬门禁但必须列为残余风险
- [ ] 最近备份年龄≤24h、保留策略为 35 天，且已在与生产 PostgreSQL/VMDK 隔离的资源完成恢复实测并记录数据库恢复耗时；没有冷备时不得把脚本或本地快照描述为主备切换证据
- [ ] 回退演练：任选一个首批应用完成停新平台 Key→切回旧系统→验证旧服务商→再迁回
- [ ] 精确最终 SHA 已显式支持 `isolated-standalone`，其 TLS overlay、25 件 secret、三域端点/ACL/AOF、整 VM 故障恢复和 release evidence 全部通过；或已改用三个独立高可用端点。禁止把单机标记为 `managed`，任一证据缺失即 No-Go

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
