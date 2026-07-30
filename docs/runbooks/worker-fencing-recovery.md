# callback/export 租约与 fencing 处置

## 观察

- `sms_worker_stalled_leases{task_kind="callback|export"}` 非零表示租约已过期、等待 due dispatcher 重新投递。
- `sms_worker_lease_events{task_kind,event}` 记录无 PII 生命周期事实。`takeover` 偶发可由 worker 退出解释；`heartbeat_lost` 或 `fencing_miss` 持续增长表示数据库连接、任务超时或并发执行异常。
- 管理端回调列表只展示稳定 `event_id`、是否停滞、租约到期时间和累计接管次数，不展示 URL、密钥、body 或手机号。

## 安全恢复

1. 先确认数据库和对应 Celery 队列健康；不要直接更新 `lease_id`、`lease_expires_at` 或文件路径。
2. 已过期 callback/export 会由固定 due dispatcher 自动重新投递；新 worker 领取时生成新 UUID，旧 worker 随后的状态写入会被 CAS 拒绝。
3. callback 达到 `dead` 后，只能由管理员使用现有“手动重推”入口；该操作清空旧租约、写审计和 `manual_retry` 租约事件。
4. export 失败后由用户重新创建导出。只有 `export_task.file_path` 指向的 `.smsx` 是已发布产物；文件名中的 UUID 必须与产生该完成 CAS 的租约一致。
5. 不得通过延长旧租约、复用 lease UUID、改写 finished 状态或手工把 `.part` 重命名为 `.smsx` 绕过恢复状态机。

## 验证

- 接管后，旧 callback/export lease 的完成更新受影响行数必须为零，并产生 `fencing_miss`。
- 重复 callback 的 body 与 `X-Sms-Event-Id` 必须使用同一稳定事件 UUID。
- 两个 export lease 的 `.part`/`.smsx` 路径必须不同；清理旧租约文件后，新租约完成文件仍存在且数据库只引用新路径。
