# callback/export 租约与 fencing 处置

## 观察

- `sms_worker_stalled_leases{task_kind="callback|export"}` 非零表示租约已过期、等待 due dispatcher 重新投递。
- `sms_worker_lease_events{task_kind,event}` 记录无 PII 生命周期事实。`takeover` 偶发可由 worker 退出解释；`heartbeat_lost` 或 `fencing_miss` 持续增长表示数据库连接、任务超时或并发执行异常。
- 管理端回调列表只展示稳定 `event_id`、是否停滞、租约到期时间和累计接管次数，不展示 URL、密钥、body 或手机号。

## 安全恢复

1. 先确认数据库和对应 Celery 队列健康；不要直接更新 `lease_id`、`lease_expires_at` 或文件路径。
2. 已过期 callback/export 会由固定 due dispatcher 自动重新投递；新 worker 领取时生成新 UUID，旧 worker 随后的状态写入会被 CAS 拒绝。
3. callback 达到 `dead` 后，只能由管理员使用现有“手动重推”入口；该操作清空旧租约、写审计和 `manual_retry` 租约事件。
4. export 失败后由用户重新创建导出。只有 `export_task.file_path` 指向的 `.smsx` 是已发布产物；文件名中的 UUID 必须与产生该完成 CAS 的租约一致。目录 `fsync` 完成前不得 `mark_done`；最终文件已形成但数据库尚未 done 时，重启后复用或由新租约重写，不得永久孤立。
5. 不得通过延长旧租约、复用 lease UUID、改写 finished 状态或手工把 `.part` 重命名为 `.smsx` 绕过恢复状态机。
6. `cleanup_exports` 对账目录与数据库：活动租约文件保留；未过期 `done` 引用缺失或不能通过终止认证时把任务标为 `failed`（不是可下载 ready）；超龄未引用 `.part/.smsx` 删除。文件删除与 `clear_file()` 任一中断后必须能重入收敛。
7. 日志只记录 `action` 与 `task_id`，不记录手机号、短信正文、Token、Key 或解密内容。

## 验证

- 接管后，旧 callback/export lease 的完成更新受影响行数必须为零，并产生 `fencing_miss`。
- 重复 callback 的 body 与 `X-Sms-Event-Id` 必须使用同一稳定事件 UUID。
- 两个 export lease 的 `.part`/`.smsx` 路径必须不同；清理旧租约文件后，新租约完成文件仍存在且数据库只引用新路径。

## Report/Reply 轮询 Redis 租约

`HeartbeatLock` 的调用方仅为 `tasks/poll_report.py` 和 `tasks/poll_reply.py`。
每次成功获取建立单调截止时间；续租从请求发出前计算新期限，超时、拒绝或 owner 丢失
立即固定本次不确定状态。迟到成功不能解除该状态；只有新一次成功 acquire 可以重置。
安全点顺序为：检查 → GetReport/GetReply → 原始响应加密捕获/提交 → 检查 → 解析。
续租故障不会取消正在读取或持久化的响应；raw 磁盘和 DB 同时异常仍走既有隔离恢复合同。

首次故障写入 `worker_lease_event`，`task_kind=poll_report|poll_reply`、`task_id=1`
表示单例轮询源，独立随机 `lease_id` 只是观测引用，不是 Redis token。
`sms_worker_lease_events{task_kind,event}` 汇总所有 worker 的数据库事实；event 只允许
`extend_rejected`、`owner_lost`、`renewal_unavailable`、`release_unavailable`、
`deadline_elapsed`。每次持有最多一次主告警，持久化计数失败保留安全日志，不能阻断 raw 捕获。
成功重新获取说明该次 acquire 成功，不推断之前一定被接管。旧 owner 释放仍由 Redis
原子 token 比较保护。这是并发抑制和安全点退出，不是严格分布式单主或 exactly-once；
重复响应的最终幂等事实继续由 PostgreSQL 约束保证。
