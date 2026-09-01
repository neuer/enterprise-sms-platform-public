# `poll_report` 独立队列拆分方案 v2

**状态：发布 A 已合入并在共享测试环境完成兼容基线验收；发布 B 已实现，待合入与共享测试环境验收。**

发布 B 的实现按 `origin/main@31abd9212a1b1274dca120fdb2fe4c58b2b848b5` 复核。
目标仅是把消费型 GetReport 轮询从短信发送槽隔离出来，不是 G2、24 小时
Locust、生产容量证明或扩容方案。任何生产动作仍必须绑定精确发布版本，并通过
`deploy/sms-compose` 受控入口执行。

## 1. 结论

采用以下架构：

- 新增独立 Celery 队列 `realtime-report`。
- 新增只监听该队列的服务 `worker-report`，并发固定为 `-c 1`。
- v1 只迁移 `app.tasks.poll_report`。
- `poll_reply`、`reconcile` 和其余 `realtime` 任务保持现状。
- 周期任务设置 `expires=report_poll_seconds`，丢弃过期调度消息，避免恢复后追赶
  历史 GetReport。
- 人工触发仍经 PostgreSQL Outbox 持久化，不设置过期时间。
- 采用两阶段兼容发布：先建立空闲消费者和完整生命周期，再切换路由。

明确否决：

- 复用 `callback` 或 `bulk` 队列。
- 让任一 worker 同时监听 `realtime,realtime-report`。
- 为本次拆队新增 Redis 实例、Redis 用户、自研 singleton/coalescing 调度器或新锁。
- 将 Send/s 是否提升作为拆队上线门禁。

## 2. 当前代码事实

| 事实 | 当前实现 | 设计影响 |
| --- | --- | --- |
| Beat 周期路由 | `poll-report` 写死到 `realtime` | 必须修改 scheduler |
| 人工触发 | API 从 Beat schedule 派生队列，再写 Outbox | 不能只改 Beat |
| Outbox 队列白名单 | 仅 `realtime` / `bulk` / `callback` | Python 与 DB CHECK 都要扩宽 |
| Broker ACL | 已允许 `~realtime*` | `realtime-report` 无需改 Redis ACL |
| 发布快照 | `_RUNTIME_SERVICES` 必须全部预先存在 | 新服务必须先建立兼容基线 |
| GetReport | 拉走即消费，先保全 raw 再解析 | 回滚不得硬杀活动任务或清队列 |

关键证据：

- `backend/app/tasks/scheduler.py`：Beat 路由与启动快照。
- `backend/app/api/ops.py`、`backend/app/services/ops_dispatch.py`：人工触发路由。
- `backend/app/services/outbox.py`、`schema.sql`：Outbox 双重队列约束。
- `deploy/redis-domain-entrypoint.sh`：broker 的 `~realtime*` ACL。
- `deploy/scripts/release_manager.py`：严格 runtime 快照与最终运行态验证。
- `backend/app/services/report_ingest.py`：raw 保全、解析与 spill 恢复顺序。

## 3. 目标数据流

```text
Beat 周期任务
  -- queue=realtime-report, expires=I --> worker-report -c 1
                                            |
                                            +--> spill 恢复
                                            +--> GetReport
                                            +--> raw 加密独立落库
                                            +--> 解析与状态回写

后台人工触发
  --> PostgreSQL Outbox(queue=realtime-report)
  --> outbox-dispatcher
  --> worker-report

短信发送
  --> realtime
  --> worker-realtime
```

其中 `I` 是发布前只读取得的生产当前 `report_poll_seconds`。拆队首发保持该值，
不假定它是 10 秒或 60 秒，也不在同一窗口修改。

## 4. 队列命名与 Redis 边界

队列固定命名为 `realtime-report`，服务固定命名为 `worker-report`。

选择 `realtime-report` 而不是 `report` 的原因：

1. 它仍是独立 Redis list/Celery queue，发送 worker 不会消费。
2. 名称命中现有 broker ACL 的 `~realtime*`，无需修改、重建或重启 Redis。
3. 现有 worker 本来就共用 broker 用户和密码，没有扩大 Redis 凭据可见面。
4. 名称长度为 15，符合 `outbox_event.queue VARCHAR(16)`。

隔离依赖精确消费者绑定，不依赖任务注册表。所有 worker 都会加载完整任务模块，
因此 `celery inspect registered` 不能证明路由隔离；必须校验 `active_queues`：

| 服务 | 唯一允许队列 |
| --- | --- |
| `worker-realtime` | `realtime` |
| `worker-report` | `realtime-report` |
| `worker-bulk` | `bulk` |
| `worker-callback` | `callback` |

## 5. 周期消息、人工触发与锁语义

### 5.1 周期消息

Beat 的 `poll-report` 调度改为：

```python
"options": {
    "queue": "realtime-report",
    "expires": report_seconds,
}
```

同时为 `app.tasks.poll_report` 配置默认 `task_routes -> realtime-report`，防止未来
直接 `.delay()` 时落入无人监听的默认队列。显式 Outbox/Beat queue 仍优先。

`expires=I` 的语义是丢弃过期的“轮询时刻”，不是删除已经从厂商拉取的响应：

- worker 停机时，Beat 可以继续产生消息，但旧消息在一个周期后过期。
- worker 恢复时，最多可能遇到一个尚未过期的旧 tick 和一个新 tick；之后应恢复
  为每周期最多一次有效调用。
- 不追赶停机期间全部历史 tick。
- 不自动清理或 purge Redis 队列。

### 5.2 人工触发

人工触发继续通过 PostgreSQL Outbox：

1. `_job_routes()` 从 Beat schedule 取得 `realtime-report`。
2. `OutboxJobSender` 持久化 `queue='realtime-report'`。
3. dispatcher 将 `app.tasks.outbox.trigger_job` 投递到新队列。
4. `worker-report` 在本地执行真正的 `poll_report`。

人工 Outbox 不设置 `expires`。如果执行时已有轮询持锁，返回 0 代表与正在执行的轮询
合并，不强制增加一次厂商调用。

### 5.3 单飞锁

保留现有 `lock:poll:report`：

- TTL 90 秒。
- heartbeat 30 秒。
- 非阻塞抢锁。
- 覆盖 spill 恢复、GetReport、raw 落库、解析和超时状态更新整个 `_poll()`。

`-c 1` 负责常规串行；Redis 锁负责发布过渡期旧 `realtime` 残留任务、人工触发及
意外双 worker 的互斥。不新增第二把锁。

## 6. `worker-report` 最小运行合同

| 项 | 值 |
| --- | --- |
| command | `celery -A app.tasks worker -Q realtime-report -c 1 -l info` |
| `SMS_COMPONENT` | `worker` |
| `DB_RUNTIME_ROLE` | `send` |
| CPU 配额 | `1.0` |
| 内存上限 | `768m` |
| DB pool | `DB_WORKER_POOL_SIZE=2`、`DB_WORKER_MAX_OVERFLOW=0` |
| 用户 | `10001:10001`；不加入 vendor-control socket 组 |
| 持久卷 | 复用现有 `rawspill` |
| 只读状态 | 保留 `vendor-test:ro`，延续现有厂商调用方生命周期合同 |

仅挂载以下 secrets：

- `vendor_secret_name`
- `vendor_secret_key`
- `data_aes_key`
- `data_hmac_key`
- `db_send_password`
- `redis_broker_password`
- `redis_control_password`

明确不挂载：

- `db_callback_password`
- `audit_context_key` 或任一 `audit_system_*_context_key`
- `redis_auth_password`
- JWT、LDAP、导出密码、告警私钥
- `vendor-control` UDS

当前 `poll_report` 正常路径不签发 system audit；报告状态、callback_task 和 Outbox
均使用现有 send-role 事务。若未来将需要 system audit 的任务路由到该 worker，必须另行
评审，不能静默扩大密钥可见面。

数据库池设为 `2+0`：一个连接供主处理路径，一个连接为 raw lease heartbeat 留余量；
新增理论连接上限为 2，不沿用通用 worker 的 `3+1`。

## 7. Outbox 与数据库迁移

必须在切换前完成：

1. `backend/app/services/outbox.py` 的 `ALLOWED_QUEUES` 增加
   `realtime-report`。
2. 手写 Alembic 迁移，将 `outbox_event.queue` CHECK 扩为：

   ```sql
   CHECK (queue IN ('realtime','realtime-report','bulk','callback'))
   ```

3. 同步回写 `schema.sql`，并使用明确约束名 `ck_outbox_queue`。
4. 更新人工任务、Outbox publisher、运维 API 和迁移契约测试。

迁移是向前兼容扩宽：

- 应用回滚不回退该 CHECK。
- 开发环境 downgrade 只有在不存在任何 `queue='realtime-report'` 记录时才允许。
- 禁止把人工 `poll_report` 特判回 `realtime`，否则管理员点击一次仍会占用发送槽。

## 8. 两阶段兼容发布

### 8.1 发布 A：兼容基础包，不切路由

发布 A 完成以下内容：

- 新增 `worker-report` Compose 服务。
- 更新 production restart 与 isolated Redis TLS client overlay。
- 更新 `sms-compose` 服务白名单与 backend 服务集合。
- 完成 Outbox allowlist、手写迁移与 `schema.sql`。
- 更新 test-update、vendor runtime reset、Mock reset、真实联调 manager、continuity、
  failover、secret 矩阵及对应契约测试，使所有停止、撤销和恢复动作都认识新服务。
- Beat 和人工触发仍指向 `realtime`；不配置新的默认 task route。

发布 A 经正式 `prepare -> activate -> status` 完成后，再通过受控
`sms-compose up -d --no-deps worker-report` 创建空闲消费者。禁止使用 raw Compose、
手工 Docker 或跳过 lifecycle lock。

建立兼容基线时必须验证：

- 容器使用 A 的精确 API 镜像。
- Celery ping 包含 `worker-report`。
- `active_queues` 只有 `realtime-report`。
- broker `realtime-report` 深度为 0。
- `outbox_event.queue='realtime-report'` 无非终态记录。
- 生产发送和 GetReport 行为仍由原 `worker-realtime` 承担。

发布 A 暂不把 `worker-report` 加入 release manager 的严格 `_RUNTIME_SERVICES`，因为
当前 `prepare` 要求集合内服务在创建前已经存在。A 与 B 必须连续安排；过渡窗口内
不进行其他应用发布或厂商凭据轮换。

### 8.2 发布 B：路由切换包

发布 B 只完成行为切换与严格运行态收口：

- Beat `poll-report` 改到 `realtime-report` 并设置 `expires=I`。
- 默认 `task_routes` 指向 `realtime-report`。
- API 人工触发随同 schedule 路由到新队列。
- 将 `worker-report` 加入 release manager 的 quiesce、backend、runtime、worker、
  recovery 与最终验证集合。
- 最终验证从“只 ping”升级为四个 worker 的 membership 加精确 `active_queues`。

此时生产基线中已存在 `worker-report`，严格 runtime 快照可以继续保持“所有服务必须
存在且容器身份精确”的原合同，无需为一次拓扑扩展引入通用 absent/present 快照协议。

B 发布期间安全性不依赖毫秒级启动顺序：broker 可以短暂保存未过期 tick，现有锁与
`expires` 保证不会并发追赶；只有 worker membership、队列绑定和新鲜轮询全部通过后，
发布才可标记成功。

## 9. 生命周期闭集

发布 A/B 合计必须覆盖以下表面，缺一不得切路由：

| 表面 | 必要更新 |
| --- | --- |
| Compose | base service、production restart、Redis TLS client overlay |
| `sms-compose` | 服务白名单、backend 集合、生产帮助文本与配置验证 |
| release manager | B 纳入 quiesce/backend/runtime/worker/recovery/final verify |
| test-update | backend 替换、回滚、运行集合和 vendor-test 状态验证 |
| 厂商凭据 | runtime reset、真实联调激活/轮换、Mock reset 的 reader 集合 |
| 连续性 | continuity 停止集合、故障切换唯一消费者与启动顺序 |
| 观测 | worker membership、`active_queues`、job/raw 新鲜度、只读 LLEN |
| 文档与测试 | secrets、Redis/部署/failover 文档及合同测试 |

生产 storage overlay 不需要修改：`rawspill` 已有固定持久化映射，不新增磁盘、目录或
volume。

## 10. 既有 raw 保全语义不得改变

本次不得修改 `ReportIngestService.poll_once()` 的关键顺序：

1. 先恢复 spill。
2. 能建立受控 raw stream 后才访问厂商。
3. 完整原始响应先 AES-GCM 加密并独立提交 `raw_vendor_log`。
4. 提交成功后才解析和更新消息状态。

响应边界继续保持：

- 不超过 4 MiB：自动解析。
- 完整的 4–64 MiB：保全为 `complete_too_large`，只允许人工恢复。
- 超过 64 MiB、配额耗尽或正文不完整：可能成为 `truncated`，不得伪装成可自动恢复。

周期消息过期不允许删除 raw、spill 或 Outbox 事实；发送 `uncertain` 也不得因拆队被
自动重发。

## 11. 验收

### 11.1 只跑必要本地门禁

新增或受影响的定向测试：

- scheduler queue、expires 与默认 task route。
- API 人工路由、Outbox queue allowlist 与 publisher。
- 手写迁移、`schema.sql` 重组和 `check_migration.py`。
- Compose secrets、volume、user、资源与排除 UDS 的合同。
- `sms-compose`、release manager、test-update、凭据 reset、Mock reset、continuity
  的服务集合。
- 四个 worker 的 exact `active_queues` 验证。

提交前只运行一次 `scripts/dev_check.sh --changed`。

明确不运行：

- G2。
- 24 小时测试。
- Locust。
- 全量 Phase B 压测。
- 与本次未改代码无关、且已在同基线通过的 raw 大套件。

### 11.2 一次短 Mock 故障演练

1. 在 `VENDOR_MOCK=1` 的共享测试环境确认新旧队列绑定。
2. 停止 `worker-report` 三个轮询周期。
3. 验证 `worker-realtime` 的普通发送仍可执行。
4. 恢复 `worker-report`。
5. 两个周期内确认新队列深度回落到 `0–1`。
6. 恢复后的首个周期最多允许一个未过期旧 tick 加一个新 tick；之后每周期最多一次。
7. 人工触发生成 `queue='realtime-report'` 的 Outbox，并由 `worker-report` 完成。
8. 一次正常 Mock GetReport 完成 raw 加密落库、解析和 `processed=true`。
9. 不出现新增 raw 完整性严重告警。

该演练只验证路由、过期和恢复语义，不是性能或生产容量证明。

### 11.3 生产 B 观察窗

观察至少 `max(5 * I, 300 秒)`，通过标准：

- 四个 worker 均在 ping membership 中，且 `active_queues` 精确匹配第 4 节。
- 至少连续 5 个周期同时出现 `poll_report=success` 和新增的
  `source='report'` raw；抢锁失败后返回 0 的“空成功”不计入这 5 次。
- 最新 `source='report'` raw 抓取时间不超过 `2 * I`。
- `realtime-report` 稳态深度为 `0–1`，无持续上升趋势。
- 无非预期 `queue='realtime-report'` 非终态 Outbox。
- `worker-report` 无重启、OOM 或数据库池超时。
- 无新增 `vendor_raw_persist_failed`、`vendor_raw_spill_failed`、
  `poll_lock_expired` 等阻断信号。
- 没有旧、新 worker 持续同时执行 GetReport 的证据。

不把 Send/s 提升、厂商 200 QPS 或 Mock 排空时间作为发布通过条件。

## 12. 失败恢复与前向回退

不能把“重新激活发布 A”写成生产方案：现有 release manager 禁止对已成功发布执行
原地 rollback，也禁止直接切回历史镜像；当前临时离线 schema v2 又不支持
`prepare-forward-rollback`。

因此分两种情况处理。

### 12.1 B 激活尚未成功

若 B 在 `activate` 内失败且尚未进入 `succeeded`，只使用该次发布自带的补偿、
`status`、`resume` 或允许的 `rollback` 路径恢复激活前的 A 运行态。不得同时手工执行
Compose、Docker 或另一次发布；`recovery_required` 时停止自动动作并保留现场。

### 12.2 B 已成功后的前向回退

若 B 已是 `succeeded`，制作新发布 R。R 是新 commit 和全新四镜像离线整包，通过普通
`prepare -> activate -> status` 发布；不复用 A/B 的历史 image ID 或 digest。

R 只恢复 A 的路由行为，不退回 A 的旧拓扑：

- Beat 的 `poll-report` 回到 `realtime`，移除本次新增的 `expires`。
- 移除 `poll_report` 的默认 `task_routes -> realtime-report`。
- 人工触发随 Beat schedule 回到 `realtime`。
- 保留 `worker-report` 服务、Outbox allowlist、扩宽后的 DB CHECK、release manager 严格
  服务集合及全部生命周期更新；`worker-report` 作为空闲消费者继续存在。
- migration target 保持当前 schema，不执行 downgrade。

执行 R 前先进入维护窗口：

1. 阻断新的人工触发，停止 Beat 并等待 Beat 租约释放。
2. 暂时保持 outbox-dispatcher 与 `worker-report` 运行，完成已经持久化的动作。
3. 等待并验证：
   - `worker-report` 无 active/reserved/scheduled 任务；
   - broker `realtime-report` 深度为 0；
   - `outbox_event.queue='realtime-report'` 无 pending/leased/published/processing；
   - `job_run` 无仍在运行的 `poll_report`。
4. 停止 outbox-dispatcher，使用全新的 R 离线整包走标准发布。R 的 release manager
   会统一重建包括 `worker-report` 在内的 backend，不需要额外手工补容器。
5. 验证 `worker-realtime` 只监听 `realtime`、`worker-report` 只监听
   `realtime-report`，且 Beat 的 `poll-report` 已回到 `realtime`。
6. 验证一次新的 `poll_report` 成功，并确认对应 `source='report'` raw 已落库。

R 的反向代码改动固定为上述三项路由变化，不引入 feature flag、双写或第二套恢复机制。

出现以下任一情况时，不得硬杀、清队列或强行推进前向回退，必须进入
`recovery_required`：

- GetReport 正在执行且无法确认响应是否已保全。
- raw/spill 状态不明确。
- 活动任务或 Outbox 无法安全排空。
- 发现两个环境或两个 worker 持续轮询同一真实厂商账号。

## 13. 与拆队分离的后续事项

拆队稳定后，以下内容必须另行评审和发布：

- 将 `report_poll_seconds` 调整为 10，并同时重启 Beat 与 API。
- API workers、PostgreSQL CPU/内存/连接池调整。
- `worker-realtime -c` 或内存调整。
- `vendor_qps`、`vendor_batch_size` 调整。
- 修正压测发生器后重新测量吞吐。
- 是否把 `poll_reply` 迁入独立队列。

这些内容不是本方案的上线前置，也不得与发布 B 同窗实施。
