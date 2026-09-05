# 应用成本限流 v1→v2 切换

`consume_send_cost` 使用 control Redis。v1 键是
`ratelimit:app:{id}:recipients:buckets` 与 `...:segments:buckets`
（字段为 unix 秒）。v2 键是 `...:recipients:v2` / `...:segments:v2`
（固定 60 槽）。新 Lua 双读后按 max 合并，只写 v2。

## Cutover

1. 发布本版本 API。不要先 `DEL` 仍在 60 秒窗口内的 v1 键。
2. 新实例用 Redis TIME 读 v1 活动窗口与 v2 环形槽；判定取 max。
3. 首次成功消耗写入 `ratelimit:app:{id}:cost:mig`
   （`schema_version=2`、`state=active`、`generation=1`）。
4. 抽干只写 v1 的旧 API 实例。旧实例看不到 v2，仍可能单独放行。
5. 所有旧 writer 退出且至少一个完整窗口过期后，v1 键可按 TTL 自然消失。
   不要用 `KEYS` 扫描清理。

## Abort

- 新版本未写入 v2、或 v2 用量仍低于 v1 时，可回滚到只写 v1 的旧二进制。
- marker 已是 `active` 且 v2 已有消耗时，禁止直接切回只读 v1：旧二进制
  会把空 v1 当零，再次获得完整额度。应先暂停新发送至少一个 60 秒窗口
  再回滚，或继续使用本版本。

## Rollback after v2 spend

1. 将 Admission 或发送入口暂停。
2. 用 Redis TIME 记录暂停起点，等待 ≥ 60 秒 + 复制裕量。
3. 确认无新旧 writer 再消耗成本键。
4. 再发布只读 v1 的版本，或继续停在 v2 双读。
5. 负向探针：v2（或暂停前）已满额的应用不得再次被放行。

`retired` / `minimum_writer_version` readiness 门禁本期未做。抽干旧实例
是安全回滚的前提。
