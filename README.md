# 企业短信管理平台

企业短信管理平台 v1.6 是一个基于 FastAPI、Vue 3、PostgreSQL、Redis 与 Celery 的短信治理平台。仓库包含 API、双前端、异步任务、数据库迁移、Mock 厂商、Docker Compose 部署合同与自动化验收。

核心安全边界包括：手机号 AES-GCM/HMAC/mask 三列保护、未知发送结果禁止自动重发、厂商拉取原文先加密落库、审计日志只增、运行凭据只从文件型 secrets 读取，以及真实联调与普通发送链路隔离。

## 本地 Mock 验证

需要 Python 3.12、Node.js 24、Docker Engine、Compose v2、OpenSSL 与 `uv`：

```bash
scripts/local_test.sh prepare
scripts/local_test.sh up
```

脚本只允许 `DEBUG=1`、`AUTH_MOCK=1`、`VENDOR_MOCK=1`，会在本机生成随机 Mock 登录密码与应用 API Key；值只写入 Git 忽略的 0600 文件，不会在终端回显。完整说明见 [本地测试文档](docs/LOCAL_TESTING.md)。

## 验证

```bash
python3 scripts/check_spec_consistency.py
python3 scripts/check_public_readiness.py
cd backend && uv run pytest -q
cd ../frontend && npm ci && npm test && npm run typecheck && npm run build
```

完整 G2 会创建并销毁专用 Mock 测试栈：

```bash
bash scripts/verify_all.sh
```

## 公开发布边界

本项目采用 source-available、保留全部权利的许可，公开可见不等于开源授权。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。仓库从无私有历史的公开根提交开始；日常开发、Secret 隔离、自动门禁和 PR 发布方式见 [PUBLICATION.md](PUBLICATION.md)。
