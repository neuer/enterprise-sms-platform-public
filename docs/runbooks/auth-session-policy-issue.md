# AD 会话策略签发世代与回滚

0102 把 AD Token 签发切到 `AuthSessionPolicy`，并作废修复前可能过宽的会话。

## 发布时发生了什么

1. `auth_session_policy.revision` 与 `min_accepted_policy_revision` 同时加 1。
2. 所有 AD 身份对应的 `user_account.security_version` 加 1。
3. API 就绪检查由 Reconciler 把新 revision 单调发布到 auth Redis。
4. 缺失 `auth_policy_version`、值为 1 的回退签发、或低于最低接受世代的 AD
   Access/Refresh 必须重新完成 AD 密码认证。

## 验收

- 当前策略 15 分钟时签发的会话，后来提高到 480 分钟，仍在原 15 分钟 Deadline 到期。
- 策略不可用时登录返回 `503 AUTH_SESSION_UNAVAILABLE`，响应不带 refresh Cookie。
- `auth_legacy_policy_fallback_total` 保持 0。

## 回滚

- 应用回滚到旧二进制前，不要 drop `min_accepted_policy_revision`。
- 0102 的 `security_version` 递增不可逆；回滚应用后用户仍需重新登录。
- 禁止把 Redis 策略改回普通 `SET`，禁止在登录请求里发布或修复策略。
