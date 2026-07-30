# AUTOPILOT.md — 无人值守执行协议（目标模式）

本协议是平台从空仓库建设到首次完整落地的历史目标模式。日常维护不再按 M0→M4 或
G0/G1/G2 重走，统一遵循 `MAINTENANCE.md`；只有明确启动全量重建或专项终局复验时才重新
启用本协议。

## 0. 目标定义（Definition of DONE）

同时满足以下三项即为完成，缺一不可：

1. TASKS.md 每个任务处于 `[x]` 已完成 或 `[⛔]` BLOCKED（BLOCKED 必须在 docs/DECISIONS.md 有编号记录，且不属于 G2 关键路径）
2. `bash scripts/verify_all.sh` 在干净环境退出码 0（G2 终局门禁）
3. 生成 `HANDOVER.md`（第 6 节规范）与 `RELEASE.md`（版本说明：已实现 FR 清单、BLOCKED 清单、已知限制）

## 1. 三层验收门禁

| 门禁 | 时机 | 内容 |
|---|---|---|
| M-1 | 首次开工 | `python3 scripts/check_spec_consistency.py` + Git/ignore/secrets 预检全绿 |
| G0 | 每个任务 | 该任务单测/断言绿 + 当前仓库中已存在的回归测试全绿 → 勾选 + commit |
| G1 | 每个里程碑 | `bash scripts/verify_milestone.sh M0|M1|M2|M3|M4` 全绿 → 打对应 tag `m0`…`m4` |
| G2 | 终局 | 干净环境（`docker compose down -v` 后）verify_all 全绿 + DONE 三条件 |

`verify_milestone.sh` 是阶段交付权威，`verify_all.sh` 是终局唯一权威。任何"我认为可以了"都不作数；门禁脚本本身的削弱性修改（跳过本阶段应有步骤、降低阈值）视为违规，只允许修复被测对象。不得在 M1 强行运行尚属于 M4 的性能/UAT 终局断言。

## 2. 无人值守环境替身（不可用真实外部依赖）

| 真实依赖 | 替身 | 规范 |
|---|---|---|
| 厂商网关 | mock_server（已有） | VENDOR_MOCK=1，契约见 docs/vendor-api.md §3 |
| AD/LDAP | **AUTH_MOCK=1** | 认证层双实现：`auth/ldap_real.py`（ldap3，单测用 monkeypatch 覆盖）与 `auth/mock.py`（校验 seed 的 dev 用户表：admin01/approver01/operator01/viewer01；密码从本机 `ldap_bind_password` secret 读取，dept 与角色见 seed-dev）。两实现共享同一接口与锁定/IP限流逻辑（锁定逻辑必须在共享层，两模式都被测到） |
| 企业微信/SMTP | **log-sink** | sys_config 渠道为空 ⇒ 告警仅落 alert_log + 结构化日志；全部告警断言基于 alert_log 行，禁止真实外呼 |
| 真实 AD 组/密钥/备机 | 移交 | 进 HANDOVER.md，不阻塞 DONE |

Dev 数据统一由 `python -m app.cli seed-dev` 创建：4 个 dev 用户、3 个应用（app-iam 仅verify / app-oa notice / app-mkt market）及其**本机随机 API Key**（仅写入 `deploy/secrets/dev-apikeys.txt`，0600 + gitignore；e2e 脚本从此文件读取）、示例模板/签名（mock 侧直接置已过审）。安全的已有 Key 会复用，旧式固定 Key 会在 seed 时自动轮换。seed-dev 仅在 `AUTH_MOCK=1` 时可执行。

## 3. 执行循环与失败协议

首次循环：M-1 预检 → 读 TASKS/PROGRESS → 从真实下一任务开始。单任务循环：读任务 → 实现+测试 → G0 → 勾选 → commit（规范见 CLAUDE.md）→ 更新 PROGRESS.md → 下一任务。**串行推进，不并行开多任务。**

M0 尚未创建 backend/frontend 时，G0 只运行当前任务可执行的断言；T0.2 起运行后端现有测试，T0.5 起同时运行前端现有测试。任何尚未存在的测试套件都不能伪装成成功，必须由任务验收明确说明“不适用”。

失败协议（防死循环）：
- 同一任务的同一验收，**修复尝试上限 3 次**（每次必须是不同假设的修复，不是重跑）
- 3 次仍红 → 该任务标 `[⛔] BLOCKED-Dnn`，在 docs/DECISIONS.md 记录：现象/三次假设与结果/影响面/建议人工动作
- BLOCKED 分级：不在 G2 关键路径（如某 UI 细节）→ 继续后续任务；在关键路径 → 先实现**满足安全红线的最小降级版**让门禁可过（降级点写入 DECISIONS + RELEASE 已知限制）；涉及密钥、手机号、重复下发、审计不可变的安全红线禁止降级，仍不可行必须终止并输出诊断报告
- 环境级故障（docker/网络/磁盘）：重试 2 次 → 记录并终止（这类只能人工）

禁止事项：跳过或注释失败测试；改门禁阈值；未在 TASKS 出现的自发重构；引入 CLAUDE.md 锁定栈之外的框架；把 BLOCKED 静默改回未完成。

## 4. 跨会话续跑（上下文丢失恢复）

任何新会话/压缩后，**第一动作**是读 `PROGRESS.md`（根目录，模板已给）。其结构固定：当前里程碑与任务、最后一次绿色 commit hash、BLOCKED 清单、下一步一句话。每完成一个任务或产生 BLOCKED 即更新并随 commit 提交。恢复流程：读 PROGRESS → `git log -3` 核对 → `pytest -x -q` 确认基线绿 → 从"下一步"继续。若 PROGRESS 与 git 状态矛盾，以 git 为准并修正 PROGRESS。

## 5. 机判验收资产（M0 即建，随功能完善）

- `scripts/verify_milestone.sh`：按 M0-M4 验证截至当前里程碑应存在的资产；不得调用尚未实现的后续里程碑断言。
- `scripts/verify_all.sh`：G2 完整门禁，步骤=规格一致性→静态/安全规则检查→单测覆盖门→迁移一致性→完整契约一致性→干净整栈→API E2E→性能→前端构建与组件测。
- `scripts/check_contract.py`：导出 FastAPI 实际 OpenAPI，与 openapi.yaml 比较 path+method、security、requestBody/parameters 与成功/错误响应 schema；任何对称差或字段差非空即 exit 1。
- `scripts/e2e_api.py`：对运行中的整栈（mock 厂商）执行 UAT 自动化子集，共 20 项（编号对应 docs/UAT.md）：05 幂等 / 06 类别越权 / 07 verify频控 / 08 时间窗deferred / 09 退订语 / 10 CONSENT_REQUIRED / 11 审批阈值+回避403 / 12 审批过期（临时5s）/ 13 计费条 / 14 黑名单+敏感词 / 15 定时取消回补 / 16 熔断+resume / 17 uncertain修复 / 18 失败重发 / 19 模板{sN}与参数超长 / 20 回调验签+dead+重推（临时1s重试）/ 24 OTP打码（查库断言）/ 25 enqueue_report unmatched / 26 anomaly（crit落alert_log）/ 27 心跳（临时缩短expect_interval）。每项独立 assert，失败打印用例号；任何临时 sys_config 必须 finally 恢复。
- `scripts/perf_smoke.py`：三阶段有界冒烟：①60s、30 RPS、verify:notice:market=2:3:5，只测 API 受理 P95<300ms；②60s、verify 1 RPS，同时以 bulk 3 RPS 饱和，测 verify 从受理到 mock Send 成功 P95<2s；③停止施压后最多 480s 排空，断言 PostgreSQL 无 queued/sending、Celery 三队列无滞留。每请求 1 个号码，mock vendor_qps=5；超时即失败。全日 10 万条大压测移交人工（HANDOVER）。
- 前端门禁：`npm run build` + `vue-tsc --noEmit` + `vitest run`（至少 SegmentBar 分段边界、PhoneMask 权限态、StatusTag 映射 3 组组件测试）。Playwright UI 冒烟为**可选** `make e2e-ui`，不进 verify_all（避免无人值守下浏览器下载/渲染抖动）。

## 6. HANDOVER.md 生成规范（终局自动生成）

固定五节：①需人工完成清单（真实 AD 组映射与 AUTH_MOCK 关闭步骤、生产 secrets 八件套、厂商 IP 主备报备与 QPS 确认、企微/SMTP 配置、冷备同步与 30min 切换实测、全日压测、真人 UAT 28 例）②生产切换步骤引用（PRD 第10章 + BOOTSTRAP §8）③BLOCKED 与降级清单（含 DECISIONS 编号）④运维速查（常用命令、告警含义、恢复动作）⑤首月观察项（计费对账、anomaly 阈值调参、unmatched 归零确认）。

## 7. Kickoff Prompt（目标模式，人类只发这一条）

```
目标：按本仓库文档无人值守完成企业短信管理平台全部开发，达到 AUTOPILOT.md 第 0 节的 DONE 定义。
规则：先执行 M-1 预检并读 PROGRESS.md 决定是全新开始还是续跑；随后严格按 AUTOPILOT.md 执行（G0/G1/G2 门禁、3 次失败即 BLOCKED 协议、跨会话续跑协议），
实现细节遵守 CLAUDE.md 全部 38 条硬性规则，环境替身按 AUTOPILOT.md 第 2 节（VENDOR_MOCK=1、AUTH_MOCK=1、告警 log-sink）。
不请求人工确认、不停待；产品歧义按最保守实现并记 DECISIONS。终局产出 HANDOVER.md 与 RELEASE.md 后停止。
```
