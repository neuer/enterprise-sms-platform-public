# 贡献指南

1. 从公开仓库的默认分支创建短生命周期分支。
2. 不提交真实手机号、邮件地址、主机地址、凭据、运行日志、截图或内部测试证据。
3. 接口或数据库变更必须同步 `openapi.yaml`、Alembic、`schema.sql` 与对应测试。
4. 涉及发送结果未知、手机号保护、审计不可变或文件型 secrets 的改动必须保持 fail closed。
5. 提交前运行：

```bash
python3 scripts/check_spec_consistency.py
python3 scripts/check_public_readiness.py
cd backend && uv run pytest -q
cd ../frontend && npm test && npm run typecheck && npm run build
```

安全缺陷不要通过公开 Issue 报告，请遵循 [SECURITY.md](SECURITY.md)。
