# 发送准入 lane 心跳

Admission 把发送故障域拆开。`/readyz` 只表示 API 进程与数据库分区探针可用，
不要求全部发送 lane 健康。单 lane 故障时 API 保持 ready，由请求准入返回
lane-specific `503 DEPENDENCY_UNAVAILABLE`。

## 故障域

| 缺失/过期组件 | verify / notice | market |
|---|---|---|
| `send-realtime` | 拒绝 `realtime_heartbeat_stale` | 按全局容量决定 |
| `send-bulk` | 按全局容量决定 | 拒绝 `bulk_heartbeat_stale` |
| `outbox-dispatcher` | 全部拒绝 `dispatcher_heartbeat_stale` | 全部拒绝 |
| 两条发送 lane 均过期 | 全部拒绝 `send_lanes_heartbeat_stale` | 全部拒绝 |

queue pause 使用同一映射：`realtime_paused` 只关 realtime，`bulk_paused` 只关 bulk。
全局 backlog / callback / dispatcher 仍是共同依赖，写在快照的 `state/reason` 上；
单 lane 心跳不把全局快照打成 CLOSED。

## 值班

告警标题优先使用：

- `realtime sender stale`
- `bulk sender stale`
- `outbox dispatcher stale`

不要用容器 ID、hostname 或 PID 当标签。生产才把缺失心跳当成 CLOSED；
开发/测试环境不会因空心跳表关发送。

## Recovery hold

`CLOSED`（`outbox_backlog` 等运行原因）或过期控制面首次回到健康
facts 时，本次快照必须是 `degraded/recovery_hold`，并在同一行写入
`hold_until`。迁移初始化的 `closed/bootstrap` 是一次性标记，全新部署
首次健康可进入 OPEN 且不建 hold。`state=open` 且带 hold 是非法组合，
数据库 CHECK 会拒绝。hold 到期前营销仍拒绝；verify/notice 可按降级
规则放行。
