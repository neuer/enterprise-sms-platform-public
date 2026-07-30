# 可执行安全验收矩阵

本文件只维护稳定的“能力 → 唯一实现 → 自动证据”映射。需求结论以 `PRD.md` 为准，
接口以 `openapi.yaml` 为准，数据模型以 `schema.sql` / Alembic 为准。日期、提交、
测试数量和 PASS/BLOCKED 快照不在仓库内重复记账：CI 事实以 GitHub 为准，生产候选
事实以变更单和 release manifest 为准。

无人值守环境固定使用 `VENDOR_MOCK=1`、`AUTH_MOCK=1` 和告警 `log-sink`；禁止请求
真实 LDAP、厂商、企业微信或 SMTP。

## 执行层级

| 层级 | 入口 | 证明内容 |
|---|---|---|
| 单元/集成 | `pytest` | 服务唯一实现、状态机、仓储 SQL、API 鉴权和 mock 契约 |
| 运行态安全 | `python3 scripts/security_acceptance.py` | Compose HTTP/DB/log 黑盒安全边界 |
| 完整契约 | `python scripts/check_contract.py openapi.yaml` | FastAPI 与 OpenAPI 字段、安全和响应零差 |
| 完整 G2 | `scripts/verify_all.sh` | 静态规则、覆盖率、迁移、干净栈、UAT、性能和前端 |

## 可靠发送与基础安全

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 加密、密钥版本、手机号 enc/hmac/mask | `services/crypto.py`、Docker secrets | `test_crypto.py`、`test_settings.py`、migration/invariants |
| 计费条、模板变量与签名 | `billing.py`、`template.py`、`sign.py` | billing/template/sign 系列测试 |
| API Key、JWT、LDAP real/mock 共享防护 | `core/apikey.py`、`core/auth/` | auth/apikey/auth_api 测试；SEC-01/02/03 |
| 分类、幂等、频控、配额、OTP 持久化打码 | `pipeline.py`、`category.py`、`freq.py`、`quota.py`、`masking.py` | send/pipeline/acceptance_controls；SEC-06 |
| 厂商适配、raw 先落地、uncertain 禁止自动重发 | `vendor/zhihui.py`、report/reconcile 服务 | vendor/report/uncertain/recovery 测试 |
| FR-05a 失败重发 | `services/resend.py`、`sms_batch.resend_of` | `test_resend.py`、messages API、完整契约；UAT 18 |

## 管控与 Web 发送

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 黑名单、敏感词、营销退订语/同意 | blacklist/sensitive/pipeline | blacklist/sensitive/pipeline_rules、Web API 测试 |
| 审批回避、过期回补、时间窗、取消/改期 | approval/scheduling | approval/scheduling/scheduler 测试 |
| Web 发送、导入逐号码三列、剔除清单无明文 | imports/import_repository | imports/web_messages/export_file 测试 |
| PostgreSQL 事实源与 Redis 对账恢复 | reconcile/queue recovery | recovery_reconcile/queue_recovery 测试 |

## 运营闭环

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 模板/签名申请、同步与厂商格式 | template/sign management + tasks | management/task/API 系列测试 |
| 余额与异常告警仅落 alert_log | balance/anomaly/alert | balance/anomaly/alert 测试，均断言 log-sink |
| 回复、退订加黑与时间线输入 | reply ingest/query/task | reply 系列测试 |
| 回调引用任务、临时 body、HMAC、重试/dead | callback services/tasks | callback 系列测试；SEC-04 保存时 SSRF，出站前 rebinding 单测 |

## 报表、审计与交付

| PRD 能力 | 事实源/唯一实现 | 自动证据 |
|---|---|---|
| 批次/号码查询、授权解密、密文导出 | batch/operations/export | query/export/API 测试；审计仅引用测试 |
| 统计、成功率、仪表盘、报表 ECharts | stats/reporting/dashboard + frontend | stats/reporting/dashboard 与前端组件测试 |
| 用户、运维中心、审计只增、任务健康 | user/admin/ops/jobtrack | user/admin/ops/jobtrack/audit coverage 测试；SEC-02/05 |
| 生命周期、owner/app 分离、迁移一致性 | housekeeping/DBA/Alembic | housekeeping/partition、迁移双空库、G2 权限检查 |
| Prometheus、部署与冷备 | metrics、deploy 文档与脚本 | metrics/deployment/failover 测试；真实本地加密恢复演练 |
| NFR-03/04 安全合规 | `scripts/security_acceptance.py` | SEC-01–07：鉴权、注入、SSRF、audit PII、OTP、日志与 secrets |

## 运行态安全命令

在 dev profile 已启动且 `python -m app.cli seed-dev` 完成后执行：

```bash
python3 scripts/security_acceptance.py \
  --base http://localhost:${API_PORT:-8000} \
  --compose-file deploy/docker-compose.yml \
  --secrets-dir deploy/secrets
```

脚本只输出 SEC 编号；不会输出 JWT、API Key、手机号、数据库命中载荷或 secret 值。它验证
viewer 无管理权限、SQL 元字符不能绕过参数化查询、loopback callback 保存前被拒、audit
无手机号载荷、verify OTP 已打码、全栈日志无手机号和已知凭据。
