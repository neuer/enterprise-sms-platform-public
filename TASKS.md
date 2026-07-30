# TASKS.md — 企业短信管理平台开发任务拆解 v1.6

> **历史建设快照：M4.4 已完成。本文件不再作为维护期任务队列，不得继续追加任务或勾选项；当前工作从 MAINTENANCE.md 与 Issue/PR 开始。**
>
> 以下内容记录建设期按里程碑执行的原始任务与验收。
> 依赖：BOOTSTRAP.md（开工引导）、PRD.md（需求）、openapi.yaml（契约）、schema.sql（模型）、docs/vendor-api.md（厂商报文与 mock 契约）、docs/UAT.md（验收用例）、docs/TRACEABILITY.md（需求追踪）、docs/ui-design.md + sms-ui-prototype.html（UI 唯一基准）、CLAUDE.md（工程约定）。
> 标注 ★v1.1 ◆v1.2 ▲v1.3 ●v1.4 ✚v1.5 ▶v1.6。
> **无人值守目标模式：门禁与失败协议以 AUTOPILOT.md 为准；本清单中带 [HANDOVER] 的验收项不在自动门禁内，终局写入 HANDOVER.md。**

---

## M0 项目初始化

- [x] ▶T0.0 执行 M-1：按 BOOTSTRAP.md 第 1 节读完全部文档；确认 Git/`.gitignore`/规格一致性门禁绿色；按第 3 节只生成 `.env` 与权威运行 secrets（此时不启动 Compose）
- [x] T0.1 仓库结构（backend/ frontend/ deploy/，见 CLAUDE.md）
- [x] T0.2 FastAPI 骨架 + settings（密钥全部走 Docker secrets 文件挂载读取★）+ /healthz
- [x] T0.5 Vue3 + Vite + Pinia + Element Plus 骨架
- [x] T0.3 以 ▲deploy/docker-compose.yml 为契约补齐 backend/frontend Dockerfile 与 nginx.conf（既有服务名/队列名/secrets 名不可改）；migrate 使用 sms_owner、运行态按七个职责角色隔离；beat 启动抢 Redis 锁★
- [⛔] BLOCKED-D01 T0.4 执行 schema.sql + Alembic 基线；★DBA 手册（owner/app 双角色 + audit_log REVOKE）；▲导入 deploy/seed.example.sql
- [x] ▶T0.6 机判资产：scripts/verify_all.sh 与 verify_milestone.sh 可执行；以 dev 依赖 PyYAML 运行 scripts/check_contract.py 做完整 OpenAPI diff；backend/scripts_support/check_migration.py；scripts/check_invariants.py；app/cli.py 的 seed-dev（4 dev 用户/3 应用固定 Key 写 deploy/secrets/dev-apikeys.txt/样例模板签名）；PROGRESS.md 纳入 git
- [x] ▶T0.7 运行 `bash scripts/verify_milestone.sh M0`，全绿后更新 PROGRESS 并打 tag m0
- **验收**：`docker compose --profile dev up -d --build` 后 /healthz 200；migrate 成功退出；七角色越权与 audit 非 INSERT 操作均被数据库拒绝、未来表不自动授权；api 容器无法读取 db_owner_password

## M1 骨架（加密 / 认证 / 网关适配 / API发送 / 报告落地轮询）

- [x] ★T1.0 加密服务 `services/crypto.py`：AES-256-GCM encrypt/decrypt、HMAC-SHA256 索引值、掩码生成；密钥+key_version 从 secrets 读取；单元测试含跨版本解密
- [x] ◆T1.0a 计费服务 `services/billing.py`：`segments = 1 if L≤70 else ceil(L/67)`，L=最终内容（含签名与退订语）；纯函数唯一实现；单测覆盖边界 70/71/134/135/500
- [x] ✚T1.0c 任务追踪基建 `core/jobtrack.py`：@tracked_job 装饰器（落 job_run：起止/耗时/items/status/error，异常不吞）；**后续全部 beat 任务必须包装**；api 进程内心跳巡检定时器（不依赖 beat）：预期间隔×2 未见新记录→job_stalled 告警，连续失败≥3→job_failed 告警
- [x] ◆T1.0b 模板渲染器 `services/template.py`：`{1}..{n}` 占位替换、参数个数校验（不符 422 TEMPLATE_PARAM_MISMATCH）、▲各参数长度 ≤ var_specs.max_len、渲染结果长度校验（≤500）、▲厂商 `{s<max_len>}` 格式转换（提交 BindTemplate 用）
- [x] T1.1 网关适配层 `vendor/zhihui.py`：▲严格按 docs/vendor-api.md 实现 8 接口（小写驼峰字段、{code,msg,data} 包络、键名 strip 容错）；超时 10s、错误码处理表落 vendor/codes.py、日志手机号强制掩码；`VENDOR_MOCK=1` 走模拟器
- [x] T1.2 厂商模拟器 `vendor/mock_server.py`：▲按 docs/vendor-api.md 第 3 节契约实现——8 接口 + `_mock/state` + callback sink；魔法号码、拉走即消费、错误/延迟、陌生report/reply与回调失败次数均可确定性注入
- [x] T1.3 认证：▶双实现（ldap_real 经 monkeypatch 单测 / AUTH_MOCK 走 seed 用户，规则31）+ 组映射 + JWT(jti)；锁定与 IP 限流在共享层★；登出/吊销/强制下线★
- [x] T1.4 API Key 中间件：prefix 查当前+宽限期旧 Key★ → SHA-256 比对 → 注入 app 上下文（含 allowed_categories★）
- [x] T1.5 应用管理：CRUD、一次性明文 Key、★rotate-key 双 Key 轮换 + revoke-old-key、★回调配置（CIDR 白名单校验、secret 加密存储）
- [x] T1.6 发送流水线 v1：校验 → ★类别策略装载（allowed_categories 越权 403）→ ◆模板渲染 → ◆营销合规（退订语自动追加/CONSENT_REQUIRED 校验）→ ◆计费条计算与配额预估 → ★幂等（Redis SETNX `idem:{app_id}:{biz_id}` EX 86400 + idempotency_record 唯一记录兜底，过期可复用）→ 去重 → 黑名单 → ◆号码级频控剔除（verify 全局 1/min+10/day、market 应用 1/day；PostgreSQL accepted 事实 + HMAC alias 主体，Redis 版本化绝对投影，app.freq_override 覆盖）→ 敏感词 → 时间窗 → ◆PostgreSQL 配额事实预留（计费条）→ ●verify OTP 等长打码（services/masking.py，入库边界，计费用打码前原文）→ 入库（phone 三列加密★）→ ★按类别投递 realtime/bulk 队列
- [x] T1.7 发送 worker×2（realtime/bulk 分队列★）：分片调 Send；★令牌桶带 realtime 预留额度（bulk 只能用 vendor_qps - reserved_realtime_qps）；5002/5003 退避；999 → balance_blocked 双队列暂停；1006 折半重试；1011 延迟 30min；★超时/网络异常 → chunk 置 uncertain，禁止自动重发
- [x] ★T1.8 报告落地与轮询：GetReport → 原始响应 AES-GCM 整体加密后先落 raw_vendor_log（另存不含手机号的 custom_ids 索引元数据）→ 受控解密逐条解析幂等回写 → 批次统计；解析异常不丢数据可重放；48h 超时置 unknown；●customId 无主的报告落 unmatched_report（三列加密，90天保留）
- [x] ★T1.9 uncertain 修复：对账时按 raw_vendor_log.custom_ids GIN 索引检索 customId，受控解密确认后修复为 submitted 并补 taskId；24h 未命中→告警转人工，人工不得直接迁移 chunk 状态
- [x] T1.10 批次/明细查询 API（明细列表返回 phone_mask★）
- **验收**：mock 下 verify 1000 条走 realtime 全部 delivered；bulk 大批次不挤占 realtime 令牌（并发压测断言 verify P95<2s★）；24h内重复 biz_id 返回同一批次，临时缩短TTL并清理后同biz_id生成新批次★；注入超时产生 uncertain 且比对修复成功★；直接 SELECT sms_message 无明文手机号★；◆150 字内容 est_segments=3 且配额扣 3×号码数；◆同号码 1 分钟内两条 verify 第二条被剔除且 removed_freq_limit=1；◆模板参数个数不符返回 422
- **M1 门禁**：`bash scripts/verify_milestone.sh M1` 全绿 → 更新 PROGRESS → tag `m1`

## M2 管控（分类策略 / 黑名单 / 敏感词 / 配额限流 / 审批 / 定时 / 对账）

- [x] T2.1 黑名单：CRUD（HMAC 主键★）+ 批量导入 + 受理过滤（Redis SET 缓存 phone_hmac★）；★按类别策略：verify 跳过、notice 看应用开关、market 强制
- [x] T2.2 敏感词：Aho-Corasick + block/audit 策略
- [x] T2.3 配额：PostgreSQL 事实账本串行预留/提交/释放，Redis 只做版本化可重建投影；驳回、过期、取消、全量剔除、入库失败、幂等复用均以 Outbox 唯一事件补偿
- [x] T2.4 应用限流：滑动窗口 每分钟受理次数
- [x] T2.5 审批：★按类别独立阈值（approval_threshold / market_approval_threshold）；★审批回避（applicant==操作者 → 403，DB CHECK 兜底）；通过入队/驳回回补；24h 过期任务
- [x] T2.6 定时与★营销时间窗：market 窗外（market_send_window）自动转次日窗口起点 scheduled + deferred_reason，响应与通知明确告知；到点扫描投递；取消/改期（改期重审）
- [x] ★T2.7 对账任务 tasks/reconcile.py：每 5min 扫描 queued/sending 无进展批次与 pending 超时分片，幂等重投（submitted/uncertain 不重投）；用量投影巡检恢复崩溃预留、记录聚合漂移并支持 Redis 清空后从 PostgreSQL 安全重建
- [x] T2.8 Web 发送页：类别选择（notice/market）、号码粘贴/★import_task+import_phone 三列引用（24h 过期、used 标记、◆单文件≤10MB/≤5万行、openpyxl read_only 流式解析）、只含 phone_mask+原因的剔除清单下载、模板渲染预览◆、调用 `/billing/preview` 的计费条与配额消耗预览、定时、◆market"已获用户同意"强制勾选、◆测试发送开关（≤5号码，豁免营销时间窗）
- [x] T2.9 审批页：待办、详情、通过/驳回（★本人提交的单不显示操作按钮）、企业微信通知
- **验收**：market 阈值 50 生效且与 notice 阈值独立★；21:30 提交 market 自动转次日 08:00★（◆is_test=true 时豁免立即发送）；审批人审自己单被拒★；kill Redis 后重启，对账 5min 内恢复队列零丢失★；◆market 未勾选同意返回 422 CONSENT_REQUIRED 且勾选行为入审计；◆无退订语内容自动追加"回T退订"并计入计费长度；◆上传 11MB 文件被拒
- **M2 门禁**：`bash scripts/verify_milestone.sh M2` 全绿 → 更新 PROGRESS → tag `m2`

## M3 运营（模板 / 签名 / 余额 / 告警 / 回复 / 回调）

- [x] T3.1 模板管理：CRUD（含 var_specs▲）+ BindTemplate 提交（▲{n}→{s<max_len>} 转换）+ GetTemplateState 定时同步（beat 每10min，仅同步 pending，checkType 0/1/2 映射▲）+ 手动同步按钮；发送时校验模板状态与内容匹配
- [x] T3.2 签名：CRUD + BindSign / GetSignState 定时与手动同步；默认签名拼接与一致性校验
- [x] T3.3 余额巡检：GetBalance → 快照 → 低阈值告警（4h 去重）
- [x] T3.4 告警服务：企业微信 + SMTP，dedup_key 去重；事件：余额低/失败率/厂商连续失败/鉴权错误/IP校验失败/★uncertain 超 24h/★回调 dead/✚job_failed/✚job_stalled/✚anomaly
- [x] ✚T3.4a 异常检测 tasks/anomaly.py：每 anomaly_scan_minutes 按 应用×类别 比对（当日 Redis 计数 vs stat_daily 近7日同时段基线）；双条件（>基线×3 且 ≥500）；verify 升 crit 并附处置建议；无基线走绝对值兜底；同日同源去重
- [x] ★T3.5 回复落地采集：GetReply → raw_vendor_log → 解析入库（三列加密）→ 关联批次；回复查询页 + 退订加黑
- [x] ★T3.6 结果回调：callback_task 只持久化批次/消息引用与无 PII 元数据（批次终态 batch.finished；message.report 按分钟聚合 ≤500 条/次，应用开关）；callback worker 投递时临时解密手机号构造 body，不回写明文；HMAC 签名（timestamp+"."+body）、5s 超时、60/300/900/3600/3600s 重试、dead 告警；管理端查询与手动重推；URL 出站前二次校验 CIDR 白名单（防 SSRF）；◆callback_secret 轮换端点（新密钥一次性明文返回）
- **验收**：mock 应用回调端验签通过★；回调端 500 → 5 次重试后 dead 并告警，手动重推成功★；余额 5000 触发告警且 4h 去重
- **M3 门禁**：`bash scripts/verify_milestone.sh M3` 全绿 → 更新 PROGRESS → tag `m3`

## M4 报表审计与交付

- [x] T4.1 查询页：◆批次列表（多条件含 is_test）+ 批次详情 + ◆跨批次号码搜索（GET /web/messages，HMAC 精确）；✚号码时间线视图（/timeline 端点：下行+回复合并事件流、日分组、回复右缩进、号码状态徽标）；部门数据权限、所有列表始终 phone_mask，approver/admin 仅通过 `/messages/{id}/phone/decrypt` 单条解密并记审计
- [x] T4.2 导出：异步 ≤10万行；★decrypted=true 需 approver/admin 且审计记录 decrypted 标记；导出文件始终 AES-GCM 密文落盘，下载接口流式解密，7 天清理
- [x] T4.3 统计聚合：stat_daily（app/dept/all × 类别★，◆含 total_segments 计费条汇总）
- [x] T4.4 仪表盘：分类别今日量与◆计费条★、成功率、余额曲线、待审批、告警、★uncertain 数、✚任务健康格（绿/红点+最近运行时间）
- [x] T4.5 报表页：日/周/月 × 应用/部门 × 类别★，◆消息数+计费条双指标，图表 + 导出
- [x] ◆T4.5a 用户管理页：AD 同步视图、角色手动覆盖（role_override）、强制下线入口
- [x] ◆T4.5b 运维中心页：告警记录（●列表端点）/ 回调任务重推 / 原始报文列表●与重放 / uncertain 分片列表● / ●unmatched 报告（查询+导出对账）/ ✚任务健康 tab（GET /admin/jobs 列表+手动触发）/ ◆队列一键恢复（force 参数▲）
- [x] T4.6 审计埋点：登录/登出/强制下线★、发送、审批、配置、★密钥轮换、◆回调密钥轮换、名单、★导出(含 decrypted)、★回调重推、★报文重放、◆队列恢复、◆角色覆盖、◆market 同意勾选；◆审计载荷禁存手机号列表（只存数量与批次引用，代码评审项）
- [x] T4.7 生命周期：分区滚动（消息/回复 12 月）；审计 36 月由 sms_owner 按 deploy/dba.md 先加密归档校验再清理，应用代码禁止删审计；★raw_vendor_log 90 天、★import_task/import_phone 24h、密文导出 7 天、✚job_run 30 天；idempotency_record 按 expires_at 持续清理
- [x] T4.8 Prometheus：双队列长度★、发送速率、厂商错误、★uncertain 数、★回调失败数、◆频控剔除数、轮询延迟
- [x] T4.9 部署文档：secrets 挂载清单★、DBA 角色手册★、备份恢复、厂商出口 IP **主备双报备**◆
- [x] ◆T4.9a 冷备方案：deploy/failover.md + 每日同步脚本 + 恢复演练脚本；RTO≤30min 实测 [HANDOVER]
- [x] T4.10 端到端验收：PRD 里程碑逐项；安全自查：密钥与明文手机号不落日志★、SQL 注入/越权、回调 SSRF 用例★、◆审计表无手机号明文抽查
- [x] ●T4.11 性能：▶scripts/perf_smoke.py 三阶段有界冒烟（30RPS API受理60s；verify 1RPS+bulk 3RPS 时延60s；停止后≤480s排空）进 verify_all；全日 10 万条 locust 大压测脚本照写但执行 [HANDOVER]
- [x] ●T4.12 UAT：▶scripts/e2e_api.py 覆盖 AUTOPILOT §5 列明的 20 项自动化断言并进 verify_all；真人全量 28 例走查 [HANDOVER]
- [x] ▶T4.13 终局产出：先运行 `bash scripts/verify_milestone.sh M4` 并打 tag m4，再跑通 G2（干净环境 verify_all 全绿）→ 自动生成 HANDOVER.md（AUTOPILOT §6 五节）与 RELEASE.md → 更新 PROGRESS.md 为 DONE
- **验收**：全量 pytest 通过；一键起全栈；◆备机切换实测 [HANDOVER]；●三阶段压测断言通过；●UAT 28例全过；●SELECT sms_batch 无连续≥4位数字的 verify 内容（OTP 打码抽查）；●构造陌生 customId 报告落 unmatched 且可按号码导出；✚kill beat 容器后 ≤2×间隔收到 job_stalled 告警；✚mock 灌入 verify 突增流量触发 crit anomaly；✚时间线页对同一号码正确合并下行与回复；演示 14 个核心场景

## M4.1 前后端页面对齐修复

- [x] T4.14 修复回复空筛选 SQL 的 asyncpg 参数类型；统一前端角色路由、菜单可见性和 401 会话失效。
- [x] T4.15 补齐应用、黑名单、敏感词三个管理员页面，覆盖 CRUD、Key/回调密钥生命周期与敏感词 block/audit 策略。
- [x] T4.16 补齐批次取消/改期/失败重发/逐条解密、批次与审计筛选、已审核模板选择/渲染预览、Bearer 剔除清单下载和 390px 布局；移除伪运行数据并拆分页面/Element Plus 资源。
- [x] T4.17 重建全新本地栈，执行四角色 18 页面与 390px 浏览器验收并重新运行 G2；504 backend tests、49 frontend tests、Ruff、Mypy、TypeScript、生产构建、安全/迁移/契约、UAT 20/20 与性能门禁全绿。

## M4.2 P0 账号体系重构

- [x] T4.18 P0 账号体系重构：建立稳定主体 / Provider / 外部身份 / 本地凭据分层模型与 Alembic/schema 契约；实现全局不区分大小写登录名空间和来源冲突审计。
- [x] T4.19 启用管理员维护的本地账号和首次强制改密，保留 AD 认证；登录页显式选择认证源且禁止自动回退，账号锁定、IP 限流和 JWT 会话策略共享。
- [x] T4.20 提供仅空系统可执行的 `init-admin`，20 位临时密码只在当前 TTY 显示一次且可由 Codex 通过 PTY 代执行；初始化与 AD 完全解耦。
- [x] T4.21 在用户管理页交付本地创建/重置/启停、Provider/凭据/同步状态与 AD 角色跟随，在系统配置页交付 AD 草稿/测试/激活/禁用和角色映射；不开放注册、重命名或硬删除。
- [x] T4.22 Provider 接口与数据模型预留未来 IAM 扩展，本期不实现 IAM；同步 OpenAPI、部署说明、规则与自动化验收。

## M4.3 单前端收敛

- [x] T4.23 将青鸾版提升为根路径唯一前端，删除经典版源码、双构建、双入口和版本切换；同步 UI 基准、部署合同与单前端测试。29 个前端测试文件/184 项、后端 2920 项、根路径容器与 390px 浏览器验收通过，退役 `/next/` 返回 410。

## M4.4 Owner PR 自动合并

- [x] T4.24 owner 同仓分支的精确 push CI 成功后，自动将唯一匹配 PR 改为 Ready 并请求 squash auto-merge；严格校验 workflow 路径、owner、同仓、非 main、分支与 head SHA，保留 required checks、会话解决和冲突保护，禁止管理员绕过。Actionlint、5 项工作流合同和 `scripts/dev_check.sh --changed` 通过。

---

## 测试要求（贯穿）

1. 单元：流水线过滤器、类别策略矩阵★、配额事实串行性/唯一释放/版本投影、错误码翻译、状态机、crypto 跨版本★、幂等★、◆billing 边界、◆频控 accepted 计数/HMAC alias/跨窗、◆模板渲染
2. 集成：mock 全链路含错误注入（频控/余额/超时★/拉走即消费★）
3. 覆盖率：services/ ≥ 80%
4. 每个 4xx 错误路径有用例；★CATEGORY_NOT_ALLOWED、审批回避 403、幂等命中、窗外 deferred；◆TEMPLATE_PARAM_MISMATCH、CONSENT_REQUIRED、频控剔除、导入超限
