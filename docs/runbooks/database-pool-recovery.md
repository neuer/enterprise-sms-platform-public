# 数据库连接池容量与恢复

## 固定预算

连接预算按 OS 进程而不是容器计算。默认 Compose 有两个 API 进程，每个最多使用
`api=8+2` 和 `metrics=2`；三个 Celery worker 各有两个 prefork child，每个 child 最多
`worker=3+1`；beat 启动读取最多 2 条并立即关闭；Outbox dispatcher 最多 2 条。理论上限
为 52，PostgreSQL `max_connections` 还必须为 migrate、owner 运维、健康检查和故障处置
预留独立余量。扩大 Uvicorn/Celery 并发前必须按相同公式重新审批，不能只调大 pool。

所有组件的 pool 获取、asyncpg 建连和 SQL 执行均有独立超时。不得把超时设为无限，不得在
JWT、dashboard、metrics、repository 或 Celery task 内调用 `create_async_engine`。

## 观测与告警

持续采集以下低基数指标：

- `sms_database_pool_connections{component,state="open|checked_out"}`；
- `sms_database_pool_budget{component}`；
- `sms_database_pool_acquisitions_total{component}`；
- `sms_database_pool_wait_seconds_total{component}`；
- `sms_database_pool_timeouts_total{component}`；
- `sms_database_pool_leaked_connections_total{component}`。

`checked_out` 长时间等于 budget、timeout 增长或 leak 非零时禁止通过扩大 overflow 掩盖。
先按 correlation ID 查慢 SQL/卡住的任务，再确认 PostgreSQL 锁、CPU、磁盘与网络状态。

## PostgreSQL 暂时不可用

1. 保持发送和 Outbox 的数据库事实边界，不清 Redis、不重放 `uncertain`、不重启数据库
   volume。
2. 确认调用在 connect/pool timeout 内失败，API 返回统一安全错误，worker 保留可重试事实。
3. PostgreSQL 恢复后观察 `pool_pre_ping` 淘汰失效连接；用健康读取和一条受控 Mock 业务流
   验证新连接可建立。
4. timeout 应停止增长，checked_out 回落；若不回落，滚动退出对应进程并要求
   `leaked_connections_total` 不增加。

## 滚动退出与 24 小时证据

候选发布前在同版本隔离环境执行 24 小时 API、worker、beat、metrics、Outbox 混合负载。
证据必须包含候选 commit、Compose 进程/并发配置、PostgreSQL `max_connections`、每组件
上述六类指标的起止值和峰值、一次短暂 PostgreSQL 断连及恢复、一次 worker child
滚动退出。通过条件：连接峰值不超过理论预算，恢复后能获取新连接，checked_out 最终归零，
timeout 只在故障窗口增长，shutdown leak 始终为零。证据不完整不得以短压测替代。
