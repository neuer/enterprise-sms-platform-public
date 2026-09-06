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

1. 关闭新发送入口（Admission `writer_cutover`）。若已有更严 CLOSED
   （例如 `outbox_backlog`），不得覆盖该原因。
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

## Abort / 回滚

- 探测失败或时间异常：保持关闭，不自动开闸。`finally` 不得无条件 OPEN。
- v2 已消费后回滚到旧二进制：必须再次停止当前 writer、排空完整窗口并
  做兼容检查。未通过则不得启动旧版本。
- 管理员绕过受支持入口直接拉起任意历史二进制，不在本协议保证范围。
