# 本地测试与登录

在仓库根目录执行：

```bash
cd <repository-root>
```

## 一键启动

首次运行或需要清空测试数据时：

```bash
scripts/local_test.sh reset
```

脚本会创建缺失的开发 `.env` 和 18 件 mock secrets，使用 dev profile 构建全栈，等待 API/mock 健康并执行幂等 `seed-dev`。已有 secret 内容不会被覆盖或打印。默认端口为：

- Web 登录：<http://localhost:18180/login>
- API 存活：<http://localhost:18100/livez>
- API 就绪：<http://localhost:18100/readyz>
- Mock vendor 状态：<http://localhost:19128/_mock/state>

如端口被占用，可只对本次命令覆盖：

```bash
WEB_PORT=28080 API_PORT=28000 MOCK_VENDOR_PORT=29028 scripts/local_test.sh up
```

后续启动保留的数据卷使用 `up`：

```bash
scripts/local_test.sh up
```

## 登录账号

四个账号共用当前本地 Mock 密码，但密码是首次 `prepare` 时随机生成的，不写入 Git：

| 用户名 | 角色 | 主要测试范围 |
|---|---|---|
| `admin01` | 管理员 | 全部菜单、系统参数、用户角色、审计、运维 |
| `approver01` | 审批员 | 待审批、授权解密、报表与查询 |
| `operator01` | 操作员 | Web 发送、批次、回复与本部门数据 |
| `viewer01` | 查看员 | 本部门只读查询与报表 |

密码由测试负责人从本机 `deploy/secrets/ldap_bind_password` 的 0600 文件通过受控渠道提供；不要把值复制到聊天、截图、Issue 或命令参数。浏览器打开 Web 登录地址，输入任一用户名与该轮密码即可。登录使用现有 `/api/v1/web/auth/login` 获取 Bearer JWT；退出会调用服务端吊销接口并清理浏览器会话。切换角色时先点击右上角“退出”，不要手工复用旧 token。

连续五次输错密码会触发账号锁定；同 IP 高频失败会触发限流。测试锁定场景后如需立即恢复干净数据，执行 `scripts/local_test.sh reset`。

## 状态与停止

```bash
scripts/local_test.sh status  # 容器、API 和 mock 健康状态
scripts/local_test.sh down    # 停止容器，保留数据卷
scripts/local_test.sh reset   # 销毁本地测试卷并重新 seed
```

查看安全日志时禁止开启 shell trace，也不要输出 `deploy/secrets/` 或 `dev-apikeys.txt` 内容：

```bash
docker compose -f deploy/docker-compose.yml logs --since=10m api worker-realtime worker-bulk worker-callback beat
```

## 安全边界

- 本地脚本只接受 `DEBUG=1`、`AUTH_MOCK=1`、`VENDOR_MOCK=1` 和 `http://mock-vendor:9028`；检测到其他值会拒绝启动。
- 测试不会请求真实 LDAP、厂商、企业微信或 SMTP；告警只写 `alert_log` 与结构化日志。
- `.env`、18 件本地测试 secrets、开发 API Key、数据库卷均是本机测试资产，已被 Git
  忽略；已有安全随机值会复用，旧式固定开发凭据会自动轮换；仍不得上传、复制到聊天或
  用于生产。
- 若要验证真实 LDAP/厂商，请按 `HANDOVER.md` 在隔离预生产执行，不能通过修改本地脚本绕过 mock-only 断言。
