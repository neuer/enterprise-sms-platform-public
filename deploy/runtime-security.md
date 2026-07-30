# 运行时认证与配置边界

## 责任分工

| 配置面 | 责任人 | 变更入口 | 合并前验证 |
|---|---|---|---|
| `ENVIRONMENT`、DEBUG/Mock、数据库池、超时 | 平台运维 | 根目录 0600 `.env` + `sms-compose` | Settings、Compose 契约与环境模式测试 |
| `sys_config` 运行参数 | 平台管理员 | Web 系统配置页 | `RuntimePolicy` 上界/跨字段校验与审计 |
| API client 路由 | 后端维护者 | 显式 `require_api_app`/`optional_api_app` dependency | `test_route_auth_contract.py` |
| Web user 路由 | 后端维护者 | 显式 Bearer dependency | `test_route_auth_contract.py` |
| 生产 API 文档 | 安全责任人 | 固定关闭，不设运行时旁路 | 生产 Settings 下 docs/ReDoc/OpenAPI 均为 `None` |

任何凭据仍只由 Docker secrets 文件提供；配置检查、日志和错误响应不得输出
secret 值、内部地址或拓扑详情。

## 环境模式

- `development`：仅单机开发/真实联调控制台，必须 `DEBUG=1 AUTH_MOCK=1`。
- `test`：仅自动化门禁，必须 `DEBUG=1 AUTH_MOCK=1 VENDOR_MOCK=1`。
- `production`：必须 `DEBUG=0 AUTH_MOCK=0 VENDOR_MOCK=0`，厂商地址必须
  HTTPS，LDAP CA 必须可读；Swagger、ReDoc、OpenAPI 全部关闭。

`ENVIRONMENT` 缺失、值未知、与安全开关矛盾或出现安全相关的未知变量时，
应用启动失败。正式启动只能经 `sms-compose`，包装器还会独立验证根 `.env`。

## 验证步骤

测试更新验证 `development`：

1. `scripts/local_test.sh prepare` 生成/校验非敏感配置。
2. `scripts/test_update.sh plan --ref origin/<branch>` 后执行
   `scripts/test_update.sh apply --ref origin/<branch>`。
3. 只有远端状态为 `verified` 且对应表面验收通过才可继续。

正式发布验证 `production`：

1. 由运维双人复核 `.env` 权限、`ENVIRONMENT=production` 与三个安全开关。
2. 执行受控 `sms-compose config` 诊断和发布门禁；不得直接运行 Compose。
3. 验证未授权访问 `/docs`、`/redoc`、`/openapi.json` 均为 404，`readyz`
   仅返回最小状态。

## 回滚边界

配置校验失败时不得改成宽松默认值，也不得临时恢复路径推断中间件或公开文档。
回滚只能切换到已通过相同环境模式门禁的上一版本镜像/提交；数据库迁移、secret
轮换和测试号码维护是独立流程，不随应用回滚自动撤销。回滚后重新执行对应环境的
配置验证和认证路由契约测试。
