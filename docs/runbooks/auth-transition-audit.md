# 认证锁定/封禁审计失败

当 Redis 已建立账号锁定或来源 IP 封禁 transition，但 PostgreSQL 审计未 ACK 时：

1. 后续拒绝由 Redis 状态快速返回 `503 AUTH_SESSION_UNAVAILABLE` 或已 ACK 后的
   `423 ACCOUNT_LOCKED` / `429 RATE_LIMITED`，不得每个请求重打数据库。
2. 审计写租约过期后由下一请求 fencing 接管；退避为 1/2/5/10/30 秒。
3. 先确认 API 实际使用 `sms_auth`，不要把 `sms_accept` 加入认证动作白名单。
4. 指标只看 `auth_transition_*_total{action=auth_account_locked|auth_ip_banned}`，
   禁止用用户名、IP 或 transition UUID 检索日志。
5. 回滚不得删除 `audit_log(action, object_id)` 唯一约束。
