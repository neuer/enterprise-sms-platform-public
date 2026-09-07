# 应用成本限流 v1→v2 切换

`consume_send_cost` 使用 control Redis。v1 键是
`ratelimit:app:{id}:recipients:buckets` 与 `...:segments:buckets`
（字段为 unix 秒）。v2 键是 `...:recipients:v2` / `...:segments:v2`
（固定 60 槽）。

**不要宣称 `max(v1,v2)` 能合并新旧 writer 的独立增量。** 静态读取仍活动的
v1 基线并折入 v2，只在旧 writer 已冻结之后有效。混跑时两边各自加数，
max 会漏计，限额会被突破。

全局切换 marker 是 `ratelimit:cost:writer_cutover`。业务请求看到空 Key
不得写成 `active`；generation/state 只由受控切换入口更新。

## 状态机

```text
preparing → old_writers_fenced → waiting_window → active_v2
                                   ↘ aborted_closed
```

字段：`schema_version`、单调 `generation`、`target_writer_version`、
`minimum_writer_version`、`fence_time`、`not_before`、`state`、
`release_binding`、`admission_reason`、`window_seconds=60`、
`safety_margin_seconds=5`。

`not_before = fence_time + 60 + 5`，时钟只用 Redis TIME。

## Cutover（方案 A）

由支持的 `test-update apply` / `deploy/scripts/writer_cutover.py` 执行，
不另建运维平台。

1. 将成本准入权威 marker 置为进行中，业务 Lua 随即拒绝新扣减。
   独立的 Send Admission（例如 `outbox_backlog`）保持其自身事实，不改写其关闭原因。
2. 用现有 compose/进程控制隔离旧 API writer，并校验探测结果。
   仅写 CLOSED 不能冒充隔离：旧二进制可能不读新 Admission 字段。
   Probe 超时、错误或仍有旧 writer 时保持关闭，不激活。
3. 确认在途旧受理结束后写入 `old_writers_fenced` 与 fence 时间。
4. 进入 `waiting_window`；Redis TIME 到达 `not_before` 前拒绝激活。
5. generation/state CAS 激活 `active_v2`。之后受支持的新发送只写同一个
   v2 计数。不要 `DEL` 活动 v1 键，不要 `KEYS`/`FLUSHALL`。
6. `sms-compose` 启动包装器对照 `deploy/writer-protocol.json` 与
   `minimum_writer_version`；缺文件视为 writer v1，予以拒绝。
   旧二进制不会自己检查该字段。

脚本按状态可重入：不重置用量、不降低 generation。marker 缺失、损坏或
与发布绑定冲突时失败关闭。

marker 缺失不证明不存在历史 writer。普通 `sms-compose up` 和局部服务重启
均不创建或重置 marker；成本扣减继续失败关闭。由持有生命周期锁的
`sms-compose test-update writer-cutover` 推进现有正式流程；窗口未到时命令
返回未完成，稍后重入同一操作。它实际停止 API 并回查运行进程，确认在途
受理结束后才开始 65 秒等待。等待中发现 API 重现或探测不确定，重新确认
隔离后重计窗口。完成后可用普通 `up` 启动目标 API。
遗留 `bootstrap` 仅校验已有合法完成状态，不具备空 marker 激活权限。

已完成同协议状态允许不同更新 ID 只读通过，不重写历史绑定、generation、
窗口或成本计数。进行中仍严格绑定操作 ID。协议实际变化时，以已完成
generation/state CAS 分配下一代；所有后续动作继续绑定该代及操作 ID。
本地 marker 是 Redis 事实的启动围栏投影，写入失败必须明确报错，不得清空
权威 marker 修复。合法旧 active marker 的历史绑定不阻断后续兼容更新。
旧 bootstrap 产生的空绑定且 `fence_time=not_before` 只作为待恢复表示读取；
业务扣减保持关闭，正式入口在原 generation 上递增并重新隔离、等待完整窗口。

## Abort / 回滚

- 探测失败或时间异常：保持关闭，不自动开闸。`finally` 不得无条件 OPEN。
- v2 已消费后回滚到旧二进制：必须再次停止当前 writer、排空完整窗口并
  做兼容检查，完成态为 `active_v1`。未通过则不得启动旧版本。
- 普通代码回滚按回滚目标 commit 的真实协议元数据判断；同为 v2 时只读
  通过，不强制降为 v1。
- 管理员绕过受支持入口直接拉起任意历史二进制，不在本协议保证范围。
