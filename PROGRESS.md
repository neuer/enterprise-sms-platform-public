# PROGRESS.md — 续跑状态（每任务完成后更新并随 commit 提交；新会话第一动作是读本文件）

- 当前里程碑：M4.1（DONE）；进入 2026-07 全库审查 Issue 整改与测试环境逐项验收。
- 当前任务：完成 P0 配额/频控事实账本的公有主仓切换部署；host-control 分类与 API 双进程容量修复已完成本地 G2，准备更新 PR #20。
- 最后完整 G2 代码 commit：`<redacted-commit>`（PR #19 run `<redacted-run>` 第 2 次 G2 与 `ci-gate` 成功，性能 P95 `0.265071s`）。
- 本轮最后绿色代码 commit：`<redacted-commit>`（PR #19 merge commit；main CI run `<redacted-run>` 成功；精确同提交 host-control 已安装并保持 inactive）。
- 活跃 BLOCKED：PR #20 原提交的 Hosted G2 两次仅在 PERF-01 失败（`0.906s`、`0.453s`，要求 `<0.300s`）；双 API worker 后本地完整同负载 P95 为 `0.0214s`。未进入远端 prepare/apply，测试 checkout/数据库仍为 `<redacted-commit>` / `0021_approval_legacy_default`，数据与 volume 未改动。
- 下一步（一句话）：提交后在干净树完成发布控制烟测并推送 PR #20，通过权威 G2 后安装精确 merge commit 快照，再执行 public cutover 至远端 `state=verified`。
- 本轮门禁状态：最新本地完整 G2 的阶段 0–9 全绿：真实 PostgreSQL 10 passed、后端 2657 passed / 12 skip、覆盖率 82.82%、迁移双建库、安全/E2E/前端均通过，PERF-01 P95 `0.0214s`；阶段 10 只因发布控制按合同要求干净 Git 树而在提交前 fail closed，提交后立即单独复验。测试随机 UUID 偶发伪装手机号的问题已改为安全分隔 nonce，PII 防线未放宽。历史 v1.6.14 四镜像/数据持久化候选 `<redacted-commit>` 的 Trivy 证据继续保留（API 0、Web 0、PostgreSQL 0、Redis 0 个 HIGH/CRITICAL）；最终不可变证据归档到生产变更单与 release manifest。
