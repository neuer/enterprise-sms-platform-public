# 测试环境专项性能压测手册

完整负载压测不进入日常 CI、定时 CI、人工 CI 或 G2 门禁。日常 CI 只跑确定性复杂度门禁
（SQL 次数、故障矩阵不变量、报告字段）。万级容量与故障恢复只在隔离测试环境由测试
负责人专项执行，结果单独归档，不得用普通 `ci-gate` 绿色状态代替性能证据。

## 分层门禁

1. **PR 快速门禁**（pytest）：`backend/tests/test_send_perf_gates.py` 与频控集合化测试
   检查万级主体解析的 SQL 上界、admission 公平性，以及容量报告不得含手机号/正文。
2. **候选版本门禁**：在真实 PostgreSQL/Redis/Celery/Nginx 环境采集指标后，用
   `scripts/perf_capacity.py` 绑定精确 Commit 与镜像摘要。10,000 recipients/request
   必须设置 `OUTBOX_POSTGRES_DSN` 或 `PERF_ALLOW_10K=1`（仅允许录入万号报告，不证明已采样）。
   按 NFR-01 判定普通受理 P95<2000ms、万级批次 P95<3000ms；P99、`sql_count`、
   `converge_s` 仍是另外的回归上限，不能代替 NFR。拒绝/失败请求必须为零。
3. **代码级故障矩阵**：`scripts/perf_fault_matrix.py --execute` 执行固定的已有业务
   pytest 节点，包括程序化依赖和隔离 PostgreSQL 回归，输出 `scope=code_regression`。
   默认不执行时是 `not_run`；缺依赖、skip、缺结果或进程失败均不能 `passed`。
   此结果不代表真实故障或 backlog 吞吐验收，后者仍在专项环境执行。场景包括 `vendor_success_response_lost`、
   `vendor_success_mark_submitted_failed`、`submitting_timeout_uncertain`、
   `redis_flush_projection_rebuild`、`worker_broker_backlog_drain`。

候选容量场景至少覆盖 `recipients_1` / `recipients_100` / `recipients_1000` /
`recipients_10000`、`frequency_new_subjects`、`frequency_hmac_alias_merge` 与
`fairness_mixed_apps`。报告字段必须含 RPS、accepted recipients/s、segments/s、
P50/P95/P99、`sql_count`、锁等待、`pool_occupancy`、`wal_bytes`、`redis_ops`、
`worker_rss_bytes`、`outbox_oldest_age` 和收敛时间，且禁止手机号或正文。

```bash
uv run --project backend python scripts/perf_capacity.py \
  --scenario recipients_10000 \
  --expected-commit "$PERF_TARGET_COMMIT" \
  --metrics-json var/perf/recipients_10000.metrics.json \
  --output var/perf/recipients_10000.report.json
```

`--expected-commit` 使用独立记录的被测版本；不自动取报告机 HEAD。metrics JSON 的
`measurement` 对象必须完整包含以下字段，版本/场景/计数不一致、无时区窗口、非有限值、
负数或错误单位均失败。报告 schema v2 的 `commit`/`image_digests` 属于被测运行态，
`reporter_commit` 单列报告工具版本。输入是外部采样声明，工具做结构和一致性校验，
不主动采样、不认证声明来源；正式归档仍需保留可复核的采集原件。

| measurement 字段 | 合同 |
|---|---|
| source | 固定 `isolated-runtime-capture`，合成/未知来源不可作为实测 |
| commit / collector_commit | 被测版本与采集器版本的完整40位SHA；被测版本等于expected-commit |
| image_digests | 非空服务名到`sha256:`摘要映射，记录全部参与运行镜像；不得写主机或Registry凭据 |
| config_sha256 | 有效配置快照的`sha256:`摘要，不写配置明文或凭据 |
| scenario / latency_unit | 与所选场景相同；延迟单位固定`ms` |
| started_at / finished_at | 含时区ISO8601，结束严格晚于开始 |
| request_count | accepted_count + rejected_count + failed_count |
| sample_count / accepted_count | 相等且为正；分位数只计受理成功样本，失败率另行判定 |
| rejected_count / failed_count | 非负整数，正式通过必须都为0 |

完整代码级矩阵通过 `SMS_PERF_FAULT_MATRIX=1 scripts/verify_vendor_postgres_recovery.sh`
执行。既有入口准备一次性迁移库，功能集成回归后在同一隔离库运行
`perf_fault_matrix.py --execute`，无需手工生成或暴露DSN。这是功能回归，不产生压力负载。
单独执行矩阵而未配置隔离库时相关场景明确为`not_run`，不能通过修改清单常量或
忽略pytest退出码制造通过。
矩阵诊断仅保留固定测试节点、状态和异常类型白名单，不输出JUnit正文、依赖错误消息或原始日志。

## 专项三阶段压测

`scripts/perf_smoke.py` 是测试环境的有界三阶段压测：阶段 1 以 30 RPS 持续 60 秒，verify:notice:market=2:3:5，API 受理要求 `P95<2000ms`；阶段 2 同时施加 verify 1 RPS 和 bulk 3 RPS 持续 60 秒，verify 从实际请求开始（含API受理）到观察到 mock Send 要求 `P95<2s`；阶段 3 停止施压后要求 PostgreSQL active 批次与 realtime/bulk/callback 队列在 `480s` 内清零。

发生器仍按默认30 RPS/60秒提交计划，但最多64个在途请求，没有无界线程池排队。
在途已满或实际开始比计划迟到超过0.25秒时停止增加负载，收尾已提交请求并判定本轮
无效；不会把目标RPS当成实发RPS。结果包含 `load_measurements` 的计划/实际开始/完成
窗口、开始/完成RPS、迟到峰值、在途峰值、失败/未开始数量，失败也输出已取得的聚合测量。
RPS分母分别是声明的计划窗口、含迟到的实际开始窗口、含尾部完成时间的完成窗口。
verify同时报告受理与受理后等待P95，但门槛仅按每个样本的完整时延计算；不得相加两个P95。
Mock Send为平台侧观测终点，不是手机收到短信。

runtime指标仅覆盖本次scrape对应的API进程，所有runtime/数据库池序列包含
`process_instance`，不得拼成跨进程连续累计值。事件循环delay是最近完成采样，另有
`event_loop_delay_peak_seconds`生命周期峰值；数据库事实缓存不缓存runtime采样。
RSS保留高水位语义，不代表当前RSS。压测结果保留该进程身份与
`runtime_memory_semantics=process_high_water_mark`，不宣称已采集全部API/Celery进程。

阶段 1 的 future scheduled 批次仅用于测量受理延迟。脚本无论成功或中途失败，均必须使用对应应用 API Key 逐批调用正式取消接口，走状态机、配额回补与审计；不得直接更新数据库。结果字段 `cancelled_scheduled_batches` 必须等于阶段 1 已受理的 scheduled 数，任何取消失败均以 `PERF-04` fail-closed，错误只报告失败数量。

正式专项压测使用默认参数，禁止通过 CLI 缩短时间或降低阈值：

```bash
uv run --project backend python scripts/perf_smoke.py \
  --base http://localhost:8000 \
  --mock-base http://localhost:9028 \
  --keys deploy/secrets/dev-apikeys.txt
```

短参数只用于开发诊断，不构成交付证据。执行脚本前必须由测试负责人确认独占的干净测试环境、完成 seed-dev，且 sys_config 为 vendor_qps=5、reserved_realtime_qps=2；`verify_all.sh` 不再准备或执行性能压测。结果只归档无PII聚合测量、进程范围、请求数、P95、排空秒数、scheduled 取消数量、Git commit 与 Compose 镜像 digest，不归档请求 body、手机号、JWT 或 API Key。

## `[HANDOVER]` 全日 Locust 10 万条

全日运行同样不进入日常 CI/G2。由性能负责人在隔离预生产执行 `scripts/locustfile.py`；该脚本固定 100000 个单号码请求、24 小时目标速率、2:3:5 类别权重，并在达到总量后停止。必须使用单用户 `-u 1`，增加用户数会按用户倍增吞吐。

API Key 文件从 seed-dev 或受控预生产密钥系统生成，只传路径 `PERF_KEYS_FILE`，权限 0600：

```bash
cd backend
PERF_KEYS_FILE=../deploy/secrets/dev-apikeys.txt \
  uv run --with 'locust>=2.32,<3' locust \
  -f ../scripts/locustfile.py \
  --host http://localhost:8000 \
  --headless -u 1 -r 1 --run-time 25h \
  --csv ../var/perf/sms-100k
```

### 开始前

1. 使用与候选版本相同的 Python 3.12/PostgreSQL 16/Redis 7/Node 24 镜像，VENDOR_MOCK=1、AUTH_MOCK=1、告警 log-sink；禁止请求真实厂商、LDAP、真实企微或 SMTP。
2. 记录数据库/Redis 初始容量、vendor_qps、reserved_realtime_qps、worker 并发、主机 CPU/内存/磁盘和网络基线。
3. 确认无其他压测、定时大批或旧 queued/sending，执行人和停止人不同且已确认 25 小时窗口。

### 运行中监控

- Locust 每分钟 RPS、失败率与受理 P50/P95/P99；失败率非零立即记录错误码分布。
- `/metrics` 十二组 family、PostgreSQL 连接/锁/磁盘、Redis 内存/队列、worker CPU/重启、uncertain 与 alert_log。
- 不得打开含 query/body 的访问日志，不得把失败响应正文或 secrets 写入报告。

### 通过与停止

- 完成数恰好 100000，HTTP 失败率为 0，受理 P95<2000ms；停止后按三阶段脚本的 480s 口径确认排空。
- 出现手机号/密钥日志、uncertain 非预期增长、数据库磁盘告急、worker 循环重启或真实外呼时立即停止并按安全事件处理。
- 报告记录 commit、镜像 digest、开始/结束时间、总量、分类别量、P50/P95/P99、失败码、资源峰值、排空时间和整改项；执行结果保持 `[HANDOVER]`，终局写入 HANDOVER.md。
