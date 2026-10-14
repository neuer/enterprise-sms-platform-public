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

## Outbox 业务等待与处理积压

`outbox_event.capacity_eligible_at` 是业务可执行时刻，和 dispatcher 的
`next_attempt_at` 基础设施重试时钟独立。回调合同内重试在同事务写入该锚点；
营销窗口延期沿原执行租约 CAS 更新。领取、发布失败、执行失败、租约恢复不能
向后移动锚点。未到期回调仍计入数量保护，但不提前累计逾期处理年龄；到期后
从该锚点持续计龄。authority busy 和任意故障退避不享受业务等待豁免。

迁移 `0125_outbox_capacity_clock` 新增可空列，NULL 永远保守解释为 created_at，
不以迁移时刻重置历史年龄。只回填能由 callback_task 的 retry/到期时间和事件
未执行状态证明的旧回调，及明确的营销窗口延期。其它旧行保持 NULL；旧 writer
仍能写入，直到切换到新 writer 前可能继续保守误拒绝。迁移需要按受保护更新
流程执行；有历史积压的大表须先核验回填查询成本与停机窗口，UPDATE 与 DDL
在同一迁移事务内；每类最多回填 10,000 行，锁等待上限 5 秒、单语句上限
30 秒，超时回滚并保持失败关闭。超过该批量或证明不足的行仍取 created_at，
不得清空积压来放行。不能把它宣称为无锁在线变更。应用镜像回退保留列及事实。

## 容量滞回、保持期限和预算

容量半阈值滞回使用 `capacity_hysteresis`，可保留 CLOSED 或 DEGRADED；
没有定时保持时 `hold_until=NULL`，不显示虚构倒计时。
`recovery_hold` 只表示带明确截止时间的 DEGRADED 保持。
不可变快照的 `budget_mode` 独立决定人数、计费条、分片和共享速率保护：
OPEN 使用 normal，CLOSED 一律拒绝，普通 DEGRADED 使用 degraded，
DEGRADED 滞回或有效 hold 使用 recovery。hold 中出现积压原因也不取消恢复预算。
CAS 败者只采纳新鲜且不更宽松的完整胜者状态，并重建预算模式。
`sms_send_admission` 读取持久化状态；过期/缺失时显示 CLOSED，不再按原始
Outbox 数量重算成 OPEN 而忽略保持期。指标不负责刷新或放行控制面状态。

`CLOSED`（含迁移初始化的 `closed/bootstrap`，以及 `outbox_backlog`
等运行原因）或过期控制面首次回到健康 facts 时，本次快照必须是
`degraded/recovery_hold`，并在同一行写入 `hold_until`。
`reason_code=bootstrap` 不豁免保持期；缺失或过期的初始化行按 CLOSED
处理，不得默认 OPEN。`state=open` 且带 hold 是非法组合，
数据库 CHECK 会拒绝。已有 hold 或容量滞回不因 15s `valid_until` 过期
重开 hold；每次仍加载新 facts，hold 到期且 facts 低于退出阈值后进入 OPEN。
旧 `degraded/recovery_hold + NULL` 保守解释为滞回；迁移 0126 原位规范化原因，
后续禁止保存无截止时间的 recovery_hold。
hold 到期前营销仍拒绝。verify/notice 使用
`recovery_max_recipients`（默认 20，不得高于普通 degraded 上限），
以及预计计费条/分片上限。超出返回稳定 reason：

- `recovery_volume`
- `recovery_segment_cost`
- `recovery_rate`

均为 `503 DEPENDENCY_UNAVAILABLE` + `Retry-After`，不要把积压数量或阈值
写进响应。多 API 实例共享 `admission:recovery:window`，按 Redis TIME 计算每秒
5 批、20 人、20 计费条预算。原因变化或状态版本递增继承同秒剩余额度，
不创建新桶。固定大小 hash 保留已观察到的最高 state_epoch，旧版本扣款失败；
即使高版本因额度已尽而被拒也推进围栏。无 TTL，避免闲置后旧版本复活；
不把最高版本作为高基数键。control Redis 不可确认或服务器时间倒退时失败关闭。

本次升级按迁移/控制面受保护流程全停切换所有 API，禁止混跑旧版 epoch 分桶
writer 与新窗口 writer。旧 Redis epoch 桶按原 TTL 自然过期。应用回退不回退
schema，不删除新窗口或控制面事实来恢复服务；旧版无期限 recovery_hold 的写入
会被 CHECK 拒绝并失败关闭，须通过受控恢复流程处理。
