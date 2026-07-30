# PRD 里程碑与安全验收矩阵

本文件是 T4.10 的可执行验收索引。需求结论仍以 PRD.md 为准，接口以 openapi.yaml 为准，数据模型以 schema.sql/Alembic 为准。无人值守环境固定使用 `VENDOR_MOCK=1`、`AUTH_MOCK=1` 和告警 `log-sink`；禁止请求真实 LDAP、厂商、真实企微或 SMTP。

## 执行层级

| 层级 | 入口 | 证明内容 |
|---|---|---|
| 单元/集成 | `pytest` | 服务唯一实现、状态机、仓储 SQL、API 鉴权和 mock 契约 |
| 运行态安全 | `python3 scripts/security_acceptance.py` | Compose HTTP/DB/log 黑盒安全边界 |
| 完整契约 | `python scripts/check_contract.py openapi.yaml` | FastAPI 与 69 个 operation 字段、安全和响应零差 |
| 里程碑/G2 | `scripts/verify_milestone.sh`、`scripts/verify_all.sh` | 静态规则、覆盖率、迁移、干净栈、UAT、性能和前端 |

## M1 骨架与可靠发送

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 加密、密钥版本、手机号 enc/hmac/mask | `services/crypto.py`、Docker secrets | `test_crypto.py`、`test_settings.py`、migration/invariants |
| 计费条、模板变量与签名 | `billing.py`、`template.py`、`sign.py` | billing/template/sign 系列测试 |
| API Key、JWT、LDAP real/mock 共享防护 | `core/apikey.py`、`core/auth/` | auth/apikey/auth_api 测试；SEC-01/02/03 |
| 分类、幂等、频控、配额、OTP 持久化打码 | `pipeline.py`、`category.py`、`freq.py`、`quota.py`、`masking.py` | send/pipeline/acceptance_controls；SEC-06 |
| 厂商适配、raw 先落地、uncertain 禁止自动重发 | `vendor/zhihui.py`、report/reconcile 服务 | vendor/report/uncertain/recovery 测试 |
| FR-05a 失败重发 | `services/resend.py`、`sms_batch.resend_of` | `test_resend.py`、messages API、完整契约；T4.12 UAT 18 |

## M2 管控与 Web 发送

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 黑名单、敏感词、营销退订语/同意 | blacklist/sensitive/pipeline | blacklist/sensitive/pipeline_rules、Web API 测试 |
| 审批回避、过期回补、时间窗、取消/改期 | approval/scheduling | approval/scheduling/scheduler 测试 |
| Web 发送、导入逐号码三列、剔除清单无明文 | imports/import_repository | imports/web_messages/export_file 测试 |
| PostgreSQL 事实源与 Redis 对账恢复 | reconcile/queue recovery | recovery_reconcile/queue_recovery 测试 |

## M3 运营闭环

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 模板/签名申请、同步与厂商格式 | template/sign management + tasks | management/task/API 系列测试 |
| 余额与异常告警仅落 alert_log | balance/anomaly/alert | balance/anomaly/alert 测试，均断言 log-sink |
| 回复、退订加黑与时间线输入 | reply ingest/query/task | reply 系列测试 |
| 回调引用任务、临时 body、HMAC、重试/dead | callback services/tasks | callback 系列测试；SEC-04 保存时 SSRF，出站前 rebinding 单测 |

## M4 报表、审计与交付

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 批次/号码查询、授权解密、密文导出 | batch/operations/export | query/export/API 测试；审计仅引用测试 |
| 统计、成功率、仪表盘、报表 ECharts | stats/reporting/dashboard + frontend | stats/reporting/dashboard 与 25 个前端组件测试 |
| 用户、运维中心、审计只增、任务健康 | user/admin/ops/jobtrack | user/admin/ops/jobtrack/audit coverage 测试；SEC-02/05 |
| 生命周期、owner/app 分离、迁移一致性 | housekeeping/DBA/Alembic | housekeeping/partition、迁移双空库、G2 权限检查 |
| Prometheus、部署与冷备 | metrics、deploy 文档与脚本 | metrics/deployment/failover 测试；真实本地加密恢复演练 |
| NFR-03/04 安全合规 | `scripts/security_acceptance.py` | SEC-01–07：鉴权、注入、SSRF、audit PII、OTP、日志与 secrets |

## T4.10 运行态安全命令

在 dev profile 已启动且 `python -m app.cli seed-dev` 完成后执行：

```bash
python3 scripts/security_acceptance.py \
  --base http://localhost:${API_PORT:-8000} \
  --compose-file deploy/docker-compose.yml \
  --secrets-dir deploy/secrets
```

脚本只输出 SEC 编号；不会输出 JWT、API Key、手机号、数据库命中载荷或 secret 值。它验证 viewer 无管理权限、SQL 元字符不能绕过参数化查询、loopback callback 保存前被拒、audit 无手机号载荷、verify OTP 已打码、全栈日志无手机号和已知凭据。

## 2026-07-13 全库审查十二项闭环

| # | 原始 finding 与闭环 | 修复提交 | 自动证据 |
|---:|---|---|---|
| 1 | 审批后仍为 scheduled 的批次不得提前入队，且不得提前回补配额 | `<redacted-id>`、`<redacted-id>` | approval、send worker 测试 |
| 2 | chunk 领取必须受 batch 合法发送状态约束 | `<redacted-id>` | send worker、SQL repository 测试 |
| 3 | Send 协议未知与陈旧 submitting 均转 uncertain，禁止自动重发 | `<redacted-id>`、`<redacted-id>` | send/recovery reconcile 测试 |
| 4 | 厂商明确失败后同事务聚合批次终态并幂等产生 finished 回调 | `<redacted-id>` | send worker、callback producer 测试 |
| 5 | HMAC 轮换期报告以全部保留版本候选匹配历史消息 | `<redacted-id>` | report ingest/repository 测试 |
| 6 | HMAC 轮换期发送与导入仍命中旧版本黑名单，候选不持久化 | `<redacted-id>` | blacklist/pipeline/import 测试 |
| 7 | 幂等回查/claim 位于限流、频控、配额和发布副作用之前，并具备续租、失败释放和 fenced 补偿 | `<redacted-id>`–`<redacted-id>` | pipeline、acceptance controls、并发/恢复测试 |
| 8 | LDAP real 强制证书验证、CA 与连接超时 | `<redacted-id>` | auth/settings 测试 |
| 9 | 只信任唯一受控代理来源，直连不能伪造客户端 IP | `<redacted-id>` | auth/deploy contract 测试 |
| 10 | Callback 保存与出站双检、固定已批准地址、限制地址切换和总墙钟超时 | `<redacted-id>`、`<redacted-id>`、`<redacted-id>`、`<redacted-id>` | app management/callback delivery 测试 |
| 11 | 报告重放使用稳定事件身份，已投递、并发与清理竞态均不重复产生副作用 | `<redacted-id>`–`<redacted-id>` | callback producer、raw replay、0012 legacy migration 测试 |
| 12 | 全部可编辑运行参数接入请求/任务边界，Admin 跨字段校验，approver/admin 全量审批 scope | `<redacted-id>`、`<redacted-id>`、`<redacted-id>` | admin/runtime/auth/import/scheduler/approval 测试 |

复审追加加固没有扩充公开 API：发送重试预算和到期时间持久化；callback 旧引用歧义升级 fail-closed；配置更新增加数据库事务最终门禁；导入与重新审批过期时间动态化；beat/API 启动快照同步任务心跳；Celery 运行策略加载隔离事件循环；callback CIDR 限定 RFC1918/ULA 私网。安全对齐复验进一步加入数据库权威的 `sys_user.auth_version`，数据版本为 schema v1.6.13、Alembic head `0014_security_hardening`，OpenAPI 仍为 69 个 operation。

本轮已重复验证：28 个定向测试文件共 231 项通过；后端全量 626 项通过；Ruff 全库、Mypy 158 个源文件、硬规则、规格与 69-operation 契约通过；真实 PostgreSQL 双建库、0012/0013 legacy upgrade/downgrade、配置并发和 callback/reconcile 跨事件循环检查通过；干净 Node 20 容器 19 个前端测试文件/49 项测试、类型检查和生产构建通过。新的完整隔离 G2 由交付主流程另行执行，本节不提前宣称 G2、PR、merge 或 tag。

## 尚未由 T4.10 宣称完成的边界

- T4.11：30 RPS API、verify/bulk 并发时延与 480 秒排空性能门禁；全日 10 万条压测为 `[HANDOVER]`。
- T4.12：AUTOPILOT 列明的 20 项自动 UAT；真人 28 例在 AUTH_MOCK=0 预生产重复为 `[HANDOVER]`。
- 冷备真实主备、DNS、厂商主/备出口 IP 切换与生产 `RTO≤30min` 实测为 `[HANDOVER]`。
- 生产 HTTPS/WAF、日志聚合平台权限、真实 AD 组、生产八件 secrets、真实企微/SMTP 均按 HANDOVER.md 执行，不以 mock 结果冒充。

## 2026-07-13 v1.6.13 生产就绪硬化验收

| 验收面 | 精确证据 | 结论 |
|---|---|---|
| 静态/契约 | 规格 24 个必需文件、57 个 API 路径、28 个 UAT；硬规则、Ruff、Mypy 158 个源文件、Alembic/schema 双建库、69 个 operation 全部通过 | PASS |
| 后端 | 审查修复后最终 `638 passed`，services 覆盖率 `86.01%`；pytest 默认 warning=`error`，仅 3 条上游 warning 按完整消息/类别/模块并以 `\Z` 封口精确豁免，输出无 warning summary | PASS |
| 依赖 | `pip-audit --strict`: `No known vulnerabilities found`；Node 24 `npm audit --omit=dev --audit-level=high`: `found 0 vulnerabilities` | PASS |
| 完整 G2 | `API_PORT=18100 MOCK_VENDOR_PORT=19128 WEB_PORT=18180 bash scripts/verify_all.sh` 退出 0；SEC-01–07、API UAT 20/20；Node 24 构建/类型/测试 20 文件 53 项全绿，卷已清理 | PASS |
| 性能冒烟 | acceptance 1800 次 P95 `19.1ms`；verify 60 次 P95 `129.7ms`；bulk 180 次；排空 `61.27s` | PASS |
| 浏览器 | 1280px/390px 下 `/dashboard`、`/send`、`/batches`、`/messages`、`/ops`、`/audit` 无文档横溢出；三类筛选卡 `scrollWidth == clientWidth`，日期控件在卡内；ops tabs 保留 `overflow-x:auto`；控制台无 warning/error | PASS |
| 可访问性 | `--tx-3=#65716b` 对 page/card/white 对比度 `4.734/5.029/5.088`；390px 真实浏览器下 nav/button/input/date/tab/pagination 已存在代表控件的 computed height 均为 `44px`；该浏览器为 fine pointer，coarse pointer 由同一 44px 媒体规则契约覆盖，>760px fine pointer 桌面密度保持 | PASS |
| HTTP 响应头 | 候选 Web 镜像 `/` 的 CSP、Permissions-Policy、X-Content-Type-Options、X-Frame-Options、Referrer-Policy 各且仅一条；内部 HTTP Nginx 不发 HSTS | PASS |
| 发布镜像 | 干净候选 `<redacted-commit>` 成功构建 API/Web；digest 固定的 Trivy 0.70.0 扫完四镜像：API 34 HIGH/2 CRITICAL（Python package target 0）、Web 35/2、PostgreSQL 48/9、Redis 16/3；门禁输出四镜像标识后退出 1 | **BLOCKED** |

发布门禁保持 fail-closed：未降低 HIGH/CRITICAL 阈值，未忽略 `affected`/`fix_deferred`，未将联网扫描塞入日常 G2。当前锁定技术栈的 `python:3.12-slim` 没有能消除这批基础层漏洞的原位升级；上线前需审批 Python/PostgreSQL 发布基础镜像方案并重跑四镜像门禁至退出 0。外部 TLS 终结器仍须提供 HTTPS 跳转及 `Strict-Transport-Security: max-age>=31536000; ...; includeSubDomains`，并完成真实 AD/厂商/告警渠道、24h 10 万条、主备 RTO 与人工 UAT。D01 不纳入本轮，`TASKS.md` 和现有安全 SQL splitter/披露均未改动。

## 2026-07-13 v1.6.14 安全基础镜像验收

| 验收面 | 精确证据 | 结论 |
|---|---|---|
| 四镜像发布门禁 | 干净候选 `<redacted-commit>` 使用 digest 固定的 Trivy 0.70.0 扫描实际部署产物：API 0、Web 0、PostgreSQL 0、Redis 0；均为 0 HIGH / 0 CRITICAL，脚本退出 0 | PASS |
| 数据镜像兼容 | `bash scripts/verify_data_images.sh` 使用临时文件 secrets 和一次性卷，验证 PostgreSQL 16 `sms_app` 非超级用户/不可建库建角色、默认权限、重启持久化，以及 Redis 7 AOF 重启持久化 | PASS |
| 镜像标识 | API `<redacted-id>…`、Web `<redacted-id>…`、PostgreSQL `<redacted-id>…`、Redis `<redacted-id>…`；生产推送后仍须归档受控仓库 RepoDigest | PASS（本地 ID） |
| 安全镜像完整 G2 | `<redacted-id>` 全新 Alpine Mock 卷：后端 644 项、services 86.01%、迁移、69-operation 契约、SEC-01–07、UAT 20/20、Node 24 前端 53 项；受理 P95 20.25ms、verify P95 121.38ms、排空 61.40s；退出 0 并清理卷 | PASS |

基础镜像代码阻塞、安全镜像完整 G2、最终审查与本地 `main` 集成已闭环。外部 TLS/HSTS、真实 AD/厂商/告警渠道、生产八件 secrets、24 小时十万条压测、主备 RTO 和真人 UAT 仍是生产发布条件。D01、`TASKS.md` 与现有安全 SQL splitter 不变。

## 2026-07-15 v1.6.15 安全与前后端契约对齐验收

| 验收面 | 精确证据 | 结论 |
|---|---|---|
| 代码集成 | 安全与契约对齐分支通过非快进 merge 集成 `main`，代码基线 `<redacted-commit>` 已推送，工作树与 `origin/main` 同步 | PASS |
| 后端与静态检查 | GitHub Actions run `<redacted-run>`：Ruff/Mypy 通过；后端 `1128 passed`，services `5328` statements、`736` miss、覆盖率 `86%` | PASS |
| 迁移与契约 | Alembic/schema 双建库与迁移检查通过；契约一致性为 69 个已实现 OpenAPI operation（完整模式） | PASS |
| 安全验收 | SEC-01 mock authentication、SEC-02 authorization scope、SEC-03 SQL injection boundaries、SEC-04 callback SSRF save boundary、SEC-05 audit payload PII、SEC-06 verify OTP persistence、SEC-07 runtime logs and secrets 全部 success | PASS |
| 自动 UAT | 固定用例 05–20、24–27 共 20 项全部 success，并完成用例级恢复 | PASS |
| 性能冒烟 | GitHub runner：acceptance 1800 次 P95 `90.30ms`；verify 60 次 P95 `605.52ms`；bulk 180 次；停止后 `66.27s` 排空 | PASS |
| 前端 | Node 24 构建、类型检查与组件测试通过；Vitest 22 个 test files、84 项 tests 全绿 | PASS |
| 四镜像发布门禁 | 相同代码基线本地运行 `bash scripts/verify_release.sh`，以只读镜像归档交给 digest 固定的 Trivy 0.70.0，API/Web/PostgreSQL/Redis 均为 0 HIGH / 0 CRITICAL，脚本退出 0 | PASS |
| hosted 候选门禁 | 候选 `<redacted-commit>` 的 CI run `<redacted-run>` 与 hosted Release Gate run `<redacted-run>` 均成功；本次文档合并后的新 HEAD 仍须重跑 | PASS（基线） |
| 远端 Mock 发布演练 | 精确修复提交 `<redacted-commit>` 完整通过 Web/API-only、数据镜像、配置失败不变、健康失败补偿与 TERM/resume；恢复后默认容器/卷最终前后快照一致，公网边界和管理员浏览器登录/退出通过。首次误清理 Mock 卷和测试环境重置已在 [演练报告](reports/2026-07-15-remote-mock-release-rehearsal.md) 披露 | PASS（控制面） |
| 候选冻结边界 | 最终文档提交不自引用自身 hash；合并后必须按其精确 `headSha` 重新执行 GitHub CI 与手动 Release Gate，最终以 hosted run 和 release evidence `candidate_commit` 归档 | 外部不可变证据 |
| 生产人工边界 | TLS/HSTS、真实 LDAP 四角色、生产八件 secrets、厂商/告警连通、受控仓库 RepoDigest、24 小时十万条、主备 RTO、真人 28 例 UAT 与生产变更单仍未完成 | HANDOVER |

本轮未降低 HIGH/CRITICAL 扫描阈值，未放宽 warning 过滤，未引入运行时外部 CDN，也未把 mock 结果描述为真实生产连通性。正式 `v*` tag 只能在 HANDOVER 人工事项签收并由最终候选 hosted Release Gate 通过后创建。
