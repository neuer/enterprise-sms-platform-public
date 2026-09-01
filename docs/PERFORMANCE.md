# 测试环境专项性能压测手册

性能压测不进入日常 CI、定时 CI、人工 CI 或 G2 门禁。所有负载测试只在隔离测试环境中
由测试负责人专项执行，结果单独归档，不得用普通 `ci-gate` 绿色状态代替性能证据。

## 专项三阶段压测

`scripts/perf_smoke.py` 是测试环境的有界三阶段压测：阶段 1 以 30 RPS 持续 60 秒，verify:notice:market=2:3:5，API 受理要求 `P95<2000ms`；阶段 2 同时施加 verify 1 RPS 和 bulk 3 RPS 持续 60 秒，verify 从受理到 mock Send 要求 `P95<2s`；阶段 3 停止施压后要求 PostgreSQL active 批次与 realtime/bulk/callback 队列在 `480s` 内清零。

阶段 1 的 future scheduled 批次仅用于测量受理延迟。脚本无论成功或中途失败，均必须使用对应应用 API Key 逐批调用正式取消接口，走状态机、配额回补与审计；不得直接更新数据库。结果字段 `cancelled_scheduled_batches` 必须等于阶段 1 已受理的 scheduled 数，任何取消失败均以 `PERF-04` fail-closed，错误只报告失败数量。

正式专项压测使用默认参数，禁止通过 CLI 缩短时间或降低阈值：

```bash
uv run --project backend python scripts/perf_smoke.py \
  --base http://localhost:8000 \
  --mock-base http://localhost:9028 \
  --keys deploy/secrets/dev-apikeys.txt
```

短参数只用于开发诊断，不构成交付证据。执行脚本前必须由测试负责人确认独占的干净测试环境、完成 seed-dev，且 sys_config 为 vendor_qps=5、reserved_realtime_qps=2；`verify_all.sh` 不再准备或执行性能压测。结果只归档请求数、P95、排空秒数、scheduled 取消数量、Git commit 与 Compose 镜像 digest，不归档请求 body、手机号、JWT 或 API Key。

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
