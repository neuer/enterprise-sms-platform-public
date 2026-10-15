# 本地测试与登录

在仓库根目录执行：

```bash
cd <repository-root>
```

## 一键启动

首次启动前确认目标是本机 Mock 环境，且不会占用其他会话的服务或数据卷。使用现有入口：

```bash
scripts/local_test.sh prepare
scripts/local_test.sh up
scripts/local_test.sh status
```

脚本会创建缺失的开发 `.env` 和 26 件 mock secrets，使用 dev profile 构建全栈，通过正式 writer 协调入口完成成本准入切换后重启 API，等待 API/mock 健康并执行幂等 `seed-dev`。已有 secret 内容不会被覆盖或打印。默认端口为：

- Web 登录：<http://localhost:18180/login>
- API 存活：<http://localhost:18100/livez>
- API 就绪：<http://localhost:18100/readyz>
- Mock vendor 状态：<http://localhost:19128/_mock/state>

如端口被占用，可只对本次命令覆盖：

```bash
WEB_PORT=28080 API_PORT=28000 MOCK_VENDOR_PORT=29028 scripts/local_test.sh up
```

`local_test.sh` 显式启用仅限 development 的 API 回环端口覆盖。基础 Compose、共享 test
及 production 默认不发布 API 宿主端口；独立开发诊断使用 `deploy/sms-compose` 时，需
显式设置 `SMS_DEV_API_PORT=1`，且实际 `.env` 的 `ENVIRONMENT` 必须是 development。
此端口授权只接受单行环境配置，`ENVIRONMENT` 必须是唯一的等号赋值；冒号、裸键、
多行值或 shell 环境冲突均拒绝，不按猜测结果开放端口。
`perf_smoke.py`、`perf_vendor_scale.py`、`security_acceptance.py` 和 `e2e_api.py` 的默认
8000 URL 只适用于该开发覆盖；其他环境按对应入口地址及授权流程执行。

入口限额回归可运行 `python scripts/verify_nginx_admission.py`：使用候选 Dockerfile 的
固定镜像摘要，在无外网、无宿主发布端口的临时容器中验证单来源/共享 NAT、全局、导入
与受信代理边界。它不启动平台、不连接厂商，也不替代完整 G2 和最终镜像漏洞扫描。

首次切换会停止 API writer，确认没有旧 writer/在途受理后，按 Redis 时间等待完整
60 秒成本窗口与 5 秒安全余量。脚本只对已确认的窗口等待作有限重试，其他协调失败立即
退出，不执行 seed 或打印整体就绪。保留数据后再次运行 `up` 可继续协调，同一版本已完成
的切换保持幂等；不要为此 reset 或手工创建 Redis marker。

本地开发的 writer 投影位于各仓库 `.test-update/writer-cutover/state.json`，启动检查与
切换共用该路径，Nginx 代理配置也默认写入 `.test-update/trusted-proxies.conf`，无需写系统目录。
该选择要求显式本地开发模式与实际 development 配置一致；共享测试、生产及固定部署入口继续使用部署专用路径。

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

密码由测试负责人从本机 `deploy/secrets/ldap_bind_password` 的 0600 文件通过受控渠道提供；不要把值复制到聊天、截图、Issue 或命令参数。浏览器打开 Web 登录地址，**先在登录页选择「AD 账号」认证源**（四个 seed 账号都是 AD 身份，页面默认选中「本地账号」，不切换会一直提示认证失败），再输入任一用户名与该轮密码即可。登录使用现有 `/api/v1/web/auth/login` 获取 Bearer access JWT，refresh 由 HttpOnly Cookie 保存；退出会调用服务端吊销接口、清除 refresh Cookie 并清理浏览器会话。切换角色时先点击右上角“退出”，不要手工复用旧 token。

连续五次输错密码会建立可恢复的账号失败标记并在当次返回 423；从未触发 IP 限流的出口提交正确凭据并完成账号绑定后会自动清除该标记，无需重置数据。同 IP 高频失败仍会独立触发限流；登录失败不需要销毁测试数据，按认证状态定位原因。

## 状态与停止

```bash
scripts/local_test.sh status  # 容器、API 和 mock 健康状态
scripts/local_test.sh down    # 停止容器，保留数据卷
```

`scripts/local_test.sh reset` 会删除本地测试卷并重新 seed；仅在操作者明确要求重建该环境数据时使用，不作为首次运行、修复测试或处理登录失败的默认步骤。

注意：本地 Mock 栈没有 vendor-control-agent 容器。管理员打开「系统参数 → 真实联调」页签时，控制状态读取会按 fail-closed 设计写入 `queue:paused:vendor-test-agent-stale:*` 安全闩锁——发送入口仍受理（queued），但 worker 停止下发。该状态现在会在「运维中心 → 队列恢复」与当前告警中可见；本地环境需要继续发送时，用 `docker compose -f deploy/docker-compose.yml exec -T redis-control sh -c 'export REDISCLI_AUTH="$(cat /run/secrets/redis_control_password)"; redis-cli --user sms_control --no-auth-warning DEL queue:paused:vendor-test-agent-stale:realtime queue:paused:vendor-test-agent-stale:bulk'` 清除（仅限本地 Mock；生产该闩锁由「真实联调」页的认证恢复流程解除，不得手工清键）。

查看安全日志时禁止开启 shell trace，也不要输出 `deploy/secrets/` 或 `dev-apikeys.txt` 内容：

```bash
docker compose -f deploy/docker-compose.yml logs --since=10m api worker-realtime worker-report worker-bulk worker-callback beat
```

## 安全边界

- 本地脚本只接受 `DEBUG=1`、`AUTH_MOCK=1`、`VENDOR_MOCK=1` 和 `http://mock-vendor:9028`；检测到其他值会拒绝启动。
- 测试不会请求真实 LDAP、厂商、企业微信或 SMTP；告警只写 `alert_log` 与结构化日志。
- `.env`、26 件本地测试 secrets、开发 API Key、数据库卷均是本机测试资产，已被 Git
  忽略；已有安全随机值会复用，旧式固定开发凭据会自动轮换；仍不得上传、复制到聊天或
  用于生产。
- 若要验证真实 LDAP/厂商，请按 `HANDOVER.md` 在隔离预生产执行，不能通过修改本地脚本绕过 mock-only 断言。
