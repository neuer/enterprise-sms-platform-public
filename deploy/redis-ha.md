# Redis 故障域、ACL 与高可用手册

## 生产拓扑

生产必须设置 `REDIS_HA_MODE=managed`，并让下列三个 host 指向三个不同的托管高可用端点。禁止把三个 DNS 名解析到同一 Redis 集群；变更复核需保存服务端实例/复制组 ID 的无敏感摘要。

| 故障域 | 客户端 ACL 用户 | 数据 | 允许消费者 | 故障语义 |
|---|---|---|---|---|
| broker | `sms_broker` | Celery 队列与结果 | workers、beat、outbox-dispatcher | PostgreSQL Outbox 保留待发布事件；不得改变认证判断 |
| auth | `sms_auth` | 登录限流、会话撤销、step-up | api | 不可用时登录、JWT 校验和高风险操作 fail closed |
| control | `sms_control` | 配额/频控/幂等/业务锁/运行投影 | api、workers、beat | PostgreSQL 事实账本不丢；恢复后重建投影 |

API 容器不挂载 `redis_broker_password`，worker-callback 不挂载 `redis_auth_password`。三个 Redis 服务关闭 default 用户，且不用 `~*` 或 `+@all`：broker 只允许 Celery 的三个业务队列及 `_kombu`/Celery 内部键，auth 只允许 `auth:*`、`export:step-up:*`、`vendor-test:step-up:*`，control 只允许已登记的配额、频控、幂等、锁和投影前缀。应用 ACL 不允许 `KEYS`，并拒绝 `ACL`、`CONFIG`、`FLUSHALL`、`FLUSHDB`、`MODULE`、`REPLICAOF`、`SHUTDOWN` 等管理命令。管理账号不进入应用 Compose；托管平台管理操作只能走受控运维身份和双人审批。

本仓库 Compose 中的三个单节点服务只用于 development/test 和镜像契约验证。生产应用连接由 `REDIS_BROKER_HOST`、`REDIS_AUTH_HOST`、`REDIS_CONTROL_HOST` 指向托管端点，密码仅由三个同名 Docker secrets 提供；禁止配置 `REDIS_URL` 或把 URI/密码写入 `.env`、Compose environment、日志、镜像层和工单。

## 托管服务基线

- 每个域至少跨两个故障区，启用自动主从切换；维护窗前验证客户端 DNS/endpoint 在切换后保持稳定。
- auth/control 启用 AOF `everysec` 或托管服务等价持久化、加密快照和跨区域副本；broker 按 Outbox 可重放边界设置同等级或更强持久化。
- `maxmemory-policy=noeviction`；达到容量阈值时明确失败并告警，不得静默驱逐会话撤销、限流或配额键。
- 禁止公网入口；安全组只允许对应应用子网。传输层使用托管服务 TLS 与 CA 校验，证书到期在 30/14/7 天告警。
- 连接预算按 API/worker/beat/outbox 进程数核算，至少保留 20% 管理与切换余量。

## 监控与阻断阈值

三个域分别采集连接数、拒绝连接、命令延迟 p95/p99、blocked clients、内存、eviction、AOF rewrite/last write、复制状态、replication lag、主从切换次数和备份年龄。任何域不得使用手机号、用户名、batch_no 或异常原文作为标签。

- eviction 非零、AOF 最近写失败、复制中断或 auth 域不可达：critical。
- replication lag 超过内部 RPO、连接使用率超过 80%、p99 超过容量基线：warning；持续两个窗口升级 critical。
- broker 故障以 Outbox backlog/dead 共同判断；auth 故障以 fail-closed 计数判断；control 故障同时核对 PostgreSQL usage ledger 与 Redis projection drift。

## 切换与恢复演练

1. 记录候选 commit、三个实例 ID、ACL 版本、最近备份对象版本和演练人/复核人，不记录 secret。
2. 在隔离测试环境制造主节点故障，确认托管端点完成切换；并发执行登录/JWT 撤销、Outbox 发布、配额预留与回补。
3. 验证 auth 故障期间旧会话不会被放行；broker flush/重启不删除 PostgreSQL Outbox；control 丢失后由 usage ledger 重建且 drift 回到零。
4. 从随机最新备份恢复到隔离实例，验证 ACL、禁用 default 用户、AOF/复制状态、key domain 和最小命令集。
5. 记录 RPO、RTO、连接峰值、p99、blocked clients、replication lag、eviction、未发布 Outbox 数和投影差异。超过目标时演练失败，禁止标记可用。

候选镜像必须运行 `scripts/verify_redis_domains.sh`，实际验证三域凭据互不通用、跨域 key/危险命令被 ACL 拒绝、default 用户关闭及 AOF 重启保留数据。生产托管服务的监控建设与主从/跨区切换验收不纳入开发测试阶段门禁，正式生产准备阶段再按本节步骤执行并留存外部证据。

## 密码轮换

三个 ACL 密码独立轮换，禁止复用或跨域回退。先在托管平台创建新凭据并复核 ACL，再原子替换对应 `0600` 权威文件，执行 `sms-compose rotate backend`；健康检查和业务探针通过后吊销旧凭据。失败时恢复同一域的旧权威文件和 ACL，不得用其他域密码临时顶替。

## 降级与回滚

- auth：保持 fail closed；不得切到 control/broker，也不得绕过撤销检查。
- broker：dispatcher 持续把失败写回 PostgreSQL Outbox 重试状态；恢复后按稳定 event ID 发布。
- control：暂停需要新配额/频控判断的写路径；恢复后从 PostgreSQL usage ledger 重建并核对 drift。
- 代码回滚不得合并 Redis 域、重新启用 default 用户或恢复 `REDIS_URL`。若托管端点回滚，仍需三个独立实例和三个独立 secrets。
