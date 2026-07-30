# TRACEABILITY.md — 需求、实现、契约与验收追踪

> 本表用于目标模式任务选取和 PR 自查。PRD 为需求源；任何新增/修改必须在同一 commit 同步 Task、OpenAPI/Schema、测试与本表。

| 需求 | 主要任务 | 数据/实现唯一点 | API 契约 | 自动/真人验收 |
|---|---|---|---|---|
| FR-00 分类策略 | T1.6/T1.7 | services/category.py, core/ratelimit.py | messages/send, web/messages/send | UAT 06/08/11/16 |
| FR-00a 计费条 | T1.0a/T1.6/T4.3 | services/billing.py | web/billing/preview, send响应 | UAT 09/13 |
| FR-01 API发送 | T1.3-T1.7 | sms_batch/chunk/message, idempotency_record | messages/send/batches | UAT 05-09/13/17 |
| FR-02 Web发送/导入 | T2.8 | import_task/import_phone | web/messages/send/import/invalid-file | UAT 10/13/14 |
| FR-03 定时 | T2.6 | sms_batch.scheduled_at | cancel/reschedule | UAT 08/15 |
| FR-04 审批 | T2.5/T2.9 | approval | approvals/decision | UAT 11/12 |
| FR-05 可靠发送 | T1.7/T1.9/T2.7 | sms_chunk, raw_vendor_log | queue/resume, uncertain列表 | UAT 16/17 |
| FR-05a 失败重发 | T1.7/T4.1 | sms_batch.resend_of | resend-failed | UAT 18 |
| FR-06 报告/raw/unmatched | T1.8/T4.5b | raw_vendor_log密文, unmatched_report | raw-logs/unmatched/export | UAT 17/25 |
| FR-07 回复 | T3.5 | sms_reply | replies, replies/{id}/blacklist | UAT 28 |
| FR-07a 回调 | T3.6 | callback_task消息引用 | callbacks/retry, webhooks | UAT 20 |
| FR-08 去重 | T1.6 | pipeline | send响应removed_duplicate | UAT 05 |
| FR-09 黑名单 | T2.1 | blacklist | admin/blacklist | UAT 14/28 |
| FR-10 敏感词 | T2.2 | services/sensitive.py | admin/sensitive-words | UAT 14 |
| FR-11 配额/限流 | T2.3/T2.4 | usage_reservation/quota_entry, services/usage_ledger.py, 版本化 Redis 投影 | send错误/preview/503失败关闭 | UAT 03/13/15 + PostgreSQL故障注入 |
| FR-11a 号码频控 | T1.6 | usage_frequency_subject/alias/entry, services/usage_ledger.py | send响应removed_freq_limit | UAT 07 + HMAC轮换/跨日/并发 |
| FR-12 模板 | T1.0b/T3.1 | sms_template, services/template.py | templates CRUD/sync | UAT 19 |
| FR-13 签名 | T3.2 | sms_sign | signs CRUD/sync | M3集成验收 |
| FR-14/15 告警 | T3.3/T3.4 | alert_log, services/alert.py | admin/alerts | UAT 12/16/20 |
| FR-25 异常检测 | T3.4a | stat_daily+Redis, anomaly.py | alerts/dashboard | UAT 26 |
| FR-27 任务健康 | T1.0c/T4.4/T4.5b | job_run, core/jobtrack.py | admin/jobs/trigger | UAT 27 |
| FR-16 查询/导出 | T4.1/T4.2 | phone_hmac, export_task密文 | batches/messages/decrypt/export | UAT 21/22 |
| FR-26 时间线 | T4.1 | sms_message+sms_reply | messages/timeline | UAT 28 |
| FR-17 统计 | T4.3-T4.5 | services/stats.py, stat_daily | reports/dashboard/stats | UAT附加抽查 |
| FR-18/18a 应用用户 | T1.5/T4.5a | app/sys_user | admin/apps/users/sessions | UAT 01-04/23 |
| FR-19 参数 | T4.5b | sys_config | admin/configs | UAT 临时参数恢复 |
| FR-20 审计 | T4.6 | audit_log只增 | admin/audit-logs | UAT 04/10/21/22/27+附加 |
| FR-21 生命周期 | T4.7 | housekeeping + owner DBA流程 | 无 | G2/移交检查 |
| FR-22/22a 加密/OTP | T1.0/T1.6/T4.2 | services/crypto.py/masking.py | decrypt/export | UAT 21/22/24/25 |
| NFR-01 性能 | T4.11 | perf_smoke.py三阶段 | — | G2性能门禁 |
| NFR-02 冷备 | T4.9a | deploy/failover.md | — | HANDOVER演练 |
| NFR-03/04 安全合规 | T0.4/T4.6/T4.10 | owner/app、secrets、加密、审计；`scripts/security_acceptance.py` | 全端错误/鉴权 | `docs/ACCEPTANCE.md` + G2 SEC-01–07 |
| NFR-05 可观测 | T4.8 | Prometheus/job_run/log/usage_projection_drift | dashboard/jobs | UAT 26/27 + 投影漂移与重建 |
| NFR-06 兼容/资源 | T0.2/T0.5 | Python3.12/PG16/Redis7/Node24 | — | 构建门禁 |
