# 全量覆盖证据与失败门禁

覆盖率只说明执行到的代码范围。授权、并发、恢复与隐私的行为断言仍独立执行；
覆盖率达标不能替代这些断言、真实环境验收或发布验证。

## 调查基线

生产源码基线为 `a5be9ffd203530a9b6d0cf1075b49b6be6fdf512`。
此前后端正式分片已收集完整 `app`，并按 application/services/auth/pipeline/export/tasks/api
分别设门槛；前端已有完整 Vitest 测试，运维脚本已有大量 Python/合同测试。
本次补齐的是覆盖数据收集、分母和消费者，不把原先未生成覆盖报告描述为没有测试。

使用 Python 3.12、coverage 7.15.0、Node 24、Vitest/coverage-v8 4.1.11。
前端在既有完整测试上增加明确 `src/**/*.{ts,vue}` 分母；仅排除生成类型和 `.d.ts`。
运维基线通过完整 unit 分片收集，`source` 为 `scripts` 和 `deploy/scripts` 的绝对路径，
启用 branch/parallel/patch=subprocess/include_namespace_packages，共 99 个 Python 文件，
没有缺失或多余文件。该基线没有把 shell 计入 Python 分母。

| 分组 | 基线语句 | 基线分支 | 新门槛：语句 / 分支 |
|---|---:|---:|---:|
| frontend（129 文件） | 7388/8600，85.91% | 6079/7297，83.31% | 85% / 82% |
| 前端 API | 1101/1211，90.92% | 586/711，82.42% | 90% / 80% |
| 前端会话 | 1061/1178，90.07% | 611/729，83.81% | 90% / 82% |
| composables | 401/421，95.25% | 183/206，88.83% | 93% / 87% |
| scripts | 6249/9787，63.85% | 1927/3558，54.16% | 63% / 53% |
| deploy/scripts | 19152/24641，77.72% | 5492/8310，66.09% | 77% / 65% |
| release_manager.py | 2746/3325，82.59% | 1042/1432，72.77% | 82% / 72% |
| restore_drill.py | 244/317，76.97% | 48/80，60.00% | 76% / 60% |
| prepare_runtime_secrets.py | 584/697，83.79% | 166/230，72.17% | 83% / 72% |
| initialize_production_storage.py | 909/1940，46.86% | 248/702，35.33% | 46% / 35% |
| storage_preflight.py | 518/663，78.13% | 165/228，72.37% | 78% / 72% |

这些新增门槛锁住完整实测基线，不宣称存储初始化已充分覆盖，也不缩小其分母。
高风险入口单独可见，不能通过其他脚本高覆盖将其平均掉。既有 app 语句门槛
75/80/80/85/80/75/70 保持不变。没有临时豁免或零门槛。

## 最终执行路径

| 范围 | 入口与进程 | 证据 | 消费者 |
|---|---|---|---|
| 前端完整组件/安全测试 | `cd frontend && npm run test:coverage`，Node 24；Python 3.12 封存 | coverage/ 下 summary、tests、manifest、lcov | `check_frontend_coverage.py verify --commit <SHA>` |
| 后端及运维 unit | `scripts/run_backend_tests.sh unit <evidence>`，Python 3.12 pytest 及受控子进程 | unit/tests.json、.coverage、coverage-manifest.json | merge 的分片绑定检查与完整测试清单检查 |
| 真实 PostgreSQL/Redis 分片 | `scripts/run_backend_tests.sh postgres <evidence>`，一次性合成库及 pytest | postgres/ 下同类证据 | 同上，不允许 skip |
| Python 汇总 | `scripts/run_backend_tests.sh merge <evidence>` | 完整 coverage.json、独立 inventory.json | check_coverage_gates.py，保留 app 消费者并新增独立运维分组 |

本地完整专项入口为 `scripts/run_backend_tests.sh full <evidence>`；CI 两分片并行，
只在均成功且 SHA 一致后合并。收集配置由 `coverage_runtime.py` 生成，不复制测试执行。
每个分片开始先废止旧成功回执；成功结束且源码未变才写出数据 SHA-256。
合并前验证提交、输入摘要、分片名称与实际覆盖数据摘要，错误或缺失一律失败。

覆盖数据使用绝对源码路径，受控 subprocess patch 将配置传入测试子进程，避免 cwd
改变导致同名脚本串计。JSON 报告可能相对报告入口 cwd 输出路径；消费者只按入口
固定的 backend 目录解释，禁止 basename 或后缀猜测，且独立核对三个源码目录全集。
`include_namespace_packages` 确保未导入脚本进入分母。

前端覆盖报告按整数组合计数重新计算，既不信任报告的百分比，也不平均文件百分比。
完整测试清单和关键会话/API 测试必须执行成功；失败、skip、缺失或重复均拒绝。
真实 Vitest 负向 fixture 复用生产配置，证明未导入文件覆盖为零且实际触发门槛失败。
Python 负向 fixture 使用两个并行子进程、不同 cwd 的同名脚本，以及未导入文件，
验证真实收集和路径归属；其余门禁测试覆盖错误 SHA、失败分片、旧数据和缺失分母。

shell、Compose、签名校验、权限和路径边界仍由既有合同与隔离集成测试验收；
不把 Python 覆盖率推广成 shell 已覆盖，更不为采样操作生产磁盘、凭据或服务。
正式结果以同 SHA CI 上传的报告为准，这里的调查基线不是最新候选的通过回执。
