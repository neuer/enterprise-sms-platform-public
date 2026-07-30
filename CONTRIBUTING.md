# 贡献指南

1. 从公开仓库的默认分支创建短生命周期分支。
2. 不提交真实手机号、邮件地址、主机地址、凭据、运行日志、截图或内部测试证据。
3. 接口或数据库变更必须同步 `openapi.yaml`、Alembic、`schema.sql` 与对应测试。
4. 涉及发送结果未知、手机号保护、审计不可变或文件型 secrets 的改动必须保持 fail closed。
5. 首次克隆运行 `scripts/install_git_hooks.sh`；提交前运行：

```bash
scripts/dev_check.sh --changed
```

6. 推送短生命周期分支。owner 分支会自动创建 Draft PR；精确 push CI 成功后，自动化
   将同一 SHA 的 PR 改为 Ready 并请求 squash merge。required `ci-gate`、会话解决与冲突
   保护仍必须满足，禁止直接推送 `main` 或管理员绕过。同仓分支 CI 由 `push` 运行一次，
   重复 PR 事件跳过；fork PR 才使用 `pull_request` 事件。
7. 测试部署前确认 `gh auth status --hostname github.com` 有效；只用系统钥匙串或官方
   设备登录，不在聊天、`.env` 或 shell profile 中保存 token。

安全缺陷不要通过公开 Issue 报告，请遵循 [SECURITY.md](SECURITY.md)。
