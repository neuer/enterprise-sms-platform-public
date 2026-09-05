# 认证锁定/封禁审计失败

当 Redis 已建立账号锁定或来源 IP 封禁 transition，但 PostgreSQL 审计未 ACK 时：

1. 后续拒绝由 Redis 状态快速返回 `503 AUTH_SESSION_UNAVAILABLE` 或已 ACK 后的
   `423 ACCOUNT_LOCKED` / `429 RATE_LIMITED`，不得每个请求重打数据库。
2. 审计写租约过期后由下一请求 fencing 接管；退避为 1/2/5/10/30 秒。
3. 先确认 API 实际使用 `sms_auth`，不要把 `sms_accept` 加入认证动作白名单。
4. 指标只看固定枚举标签的 `auth_transition_*`，禁止用用户名、IP 或 transition UUID
   检索日志或指标。
5. 回滚不得删除 `audit_log(action, object_id)` 唯一约束。

## Envelope 与 Due 索引

Canonical 事实是 `auth:audit:transition:<id>` Hash。`auth:audit:due` 只是可重建调度
索引，`auth:audit:open` 只跟踪非终态 ID。首次锁定/封禁必须在同一 Lua 内写全不可变
信封、状态和 Due；后续请求只能 `claim_existing`，不得用当前请求补写 Action、
Provider、IP、Count。

`pending` / `writing` 使用 `PERSIST`，不得靠 24 小时 TTL 自动消失。`audited` /
`dead` / `orphaned` 再设置 24 小时终态保留。认证 Redis 必须 `noeviction`；应用
ACL 不能 `CONFIG GET`，部署预检看 `deploy/redis-domain-entrypoint.sh`。

## 孤儿与死信

Due 成员没有完整信封时：

1. 不得 `HSET` 重建，不得写 `auth_account_locked` / `auth_ip_banned`；
2. 原子 `ZREM` Due/Open，Hash 标 `orphaned`（若还在）；
3. 写 Redis `auth:audit:dead-letter:<id>` 与 PostgreSQL
   `auth_transition_dead_letter`（只含 transition HMAC、reason、field_class、
   发现时间、构建版本）；
4. 打 `auth_transition_orphan_total` / `auth_transition_dead_letter_total`，
   并按 `auth_transition` crit 告警。

Hash 在、Due 不在时，用信封里的 `next_retry_at_ms` / `lease_expires_ms` /
`created_at_ms` 重建调度，不得把 `created_at_ms` 改成当前时间。

运维只可用 `sms_owner` 查死信 HMAC；`sms_auth` 只有 INSERT。原始 UUID、用户名、
密码、Token 不得进死信或告警标签。
