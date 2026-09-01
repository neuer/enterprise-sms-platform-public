# Redis 故障域、ACL 与高可用手册

## 生产模式与目标拓扑

生产必须显式设置 `REDIS_HA_MODE=managed` 或
`REDIS_HA_MODE=isolated-standalone`；`standalone` 仅限 development/test。两种生产模式都要求
broker/auth/control 使用三个不同 host:port 端点、三个独立 ACL 密码和 TLS 主机名校验。

`managed` 只用于三个独立托管高可用端点。禁止把三个 DNS 名解析到同一 Redis 集群；变更
复核需保存服务端实例/复制组 ID 的无敏感摘要。

Phase 0 选择 `isolated-standalone`：在承载 Core 与 PostgreSQL 的同一台生产 VM 内运行三个
独立非 HA Redis 容器。正式入口
必须按该模式叠加受控 TLS/持久化 Compose 合同，三个端点不可重复；第 25 件 canonical secret
`redis_tls_server_key` 只进入 `current/redis`，不得进入 backend。客户端仍只读取 CA 和三域
各自密码，使用 `rediss://` 并校验证书链与主机名。

不得把此单机形态写成 `managed`。精确最终 SHA 的 settings 校验、Compose 展开、25 件 secret
inventory、ACL/TLS/持久化测试、整 VM 故障演练和正式 release evidence 任一缺失，均为发布
**No-Go**。文档契约测试只证明风险和决策被记录，不证明运行态已经满足该拓扑。

### isolated-standalone TLS 材料

内部 PKI 必须签发一张带 `serverAuth` EKU 且 SAN 至少精确包含 `redis`、`redis-auth`、
`redis-control` 的服务端证书。宿主文件固定为：

| 文件 | owner/mode | 用途 |
|---|---|---|
| `/etc/sms-platform/redis-tls/ca.pem` | `root:root 0644` | 所有 Redis 客户端和服务端只读 CA bundle |
| `/etc/sms-platform/redis-tls/server.pem` | `root:root 0644` | 三个 Redis 容器共享的服务端证书；不得包含私钥 |
| `deploy/secrets/redis_tls_server_key` | `root:root 0600` | 与证书配对的无口令 PKCS#8 私钥，只进入 Redis runtime generation |

正式入口在任何 Compose 变更前运行 `deploy/scripts/redis_tls_preflight.py`，验证文件非链接、
owner/mode、当前有效且至少剩余 7 天、serverAuth、三个 SAN、CA 链及 key/cert 配对。任一失败
立即 No-Go，不得通过关闭 hostname check、改用系统默认 CA 或临时启用明文端口绕过。

`rotate backend` 只支持 ACL/应用凭据轮换，必须逐字拒绝 CA、server certificate 或
`redis_tls_server_key` 任一变化；它的旧 secret generation 不能回退宿主公开 TLS 文件，禁止用于
TLS 轮换。TLS 轮换须取得全平台停机窗口：先停止业务与 lifecycle timers，保存旧三件材料的
受控副本并执行 `sms-compose down`；再分别以同目录临时文件、正确 owner/mode 和 `rename` 替换
CA、certificate、canonical private key，运行整套预检后通过普通受控 `up` 重建三 Redis 与全部
客户端。失败时再次停全栈，**同时**恢复三件旧材料、重跑预检并受控启动；禁止只回退 key、只
回退证书、分批重建或绕过 release/start gate。三个文件跨目录不是单一原子事务，因此整个窗口
始终保持入口与发送面关闭。

| 故障域 | 客户端 ACL 用户 | 数据 | 允许消费者 | 故障语义 |
|---|---|---|---|---|
| broker | `sms_broker` | Celery 队列与结果 | workers、beat、outbox-dispatcher | PostgreSQL Outbox 保留待发布事件；不得改变认证判断 |
| auth | `sms_auth` | 登录限流、会话撤销、step-up | api | 不可用时登录、JWT 校验和高风险操作 fail closed |
| control | `sms_control` | 配额/频控/幂等/业务锁/运行投影 | api、workers、beat | PostgreSQL 事实账本不丢；恢复后重建投影 |

## Phase 0 单生产 VM 三实例风险接受边界

三个 standalone 实例必须满足下列隔离合同；它们还与 Core、Nginx 和 PostgreSQL 共享同一 VM、
hypervisor 路径、宿主内核、电源和维护窗口，三者共享 Redis VMDK，属于**单 VM 共同故障域**，不得
描述为高可用、跨故障域或主从切换：

| 域 | 必须独立 | 同 VM 无法消除的失败 |
|---|---|---|
| broker | 容器/端口、配置、AOF 目录、ACL 用户/密码、资源上限 | VM/宿主重启会停止 Celery 投递；恢复依赖 PostgreSQL Outbox |
| auth | 容器/端口、配置、AOF 目录、ACL 用户/密码、资源上限 | VM 故障使登录、JWT 撤销/轮换和 step-up fail closed |
| control | 容器/端口、配置、AOF 目录、ACL 用户/密码、资源上限 | VM 故障使配额、频控、幂等与锁不可确认，相关写路径返回 503 |

三个实例必须关闭 default 用户，拒绝跨域凭据、跨域 key 和危险管理命令；使用
`noeviction`、AOF `everysec`、三个独立 AOF 持久化目录和启动顺序检查。Redis AOF 只支持同 VM
进程重启，不是平台灾备快照；整机恢复以 PostgreSQL 事实源、Outbox 和用量账本为准。三个
容器当前使用镜像内相同数值 UID/GID `999:1000`，隔离来自容器/挂载命名空间、独立目录和 ACL，
不得宣称独立 Unix 用户；这些措施只能降低误操作与横向越权，不能改变共同故障域结论。恢复演练必须模拟整台生产 VM 丢失，而不只是
重启某一个进程，并记录 auth fail-closed、Outbox 回放、control 投影重建、数据缺口与恢复
时间。

短信发送能力 business-RTO≤12h、平台数据 RPO≤24h 是 Phase 0 业务/数据目标，不等于 Redis
三实例各自具备同等持久性。旧系统受控切回可停止 business-RTO；整 VM/新平台恢复耗时必须
单列 `platform_recovery_elapsed`。当前没有
托管 PostgreSQL、KMS、跨机房备份或独立 Redis HA，任何静态配置、单元测试或 AOF 文件存在
都不能证明上述目标已经实现。

API 容器不挂载 `redis_broker_password`，worker-callback 不挂载 `redis_auth_password`。三个 Redis 服务关闭 default 用户，且不用 `~*` 或 `+@all`：broker 只允许 Celery 的 `realtime`、`realtime-report`、`bulk`、`callback` 四个业务队列及 `_kombu`/Celery 内部键，auth 只允许 `auth:*`、`export:step-up:*`、`vendor-test:step-up:*`，control 只允许已登记的配额、频控、幂等、锁和投影前缀。应用 ACL 不允许 `KEYS`，并拒绝 `ACL`、`CONFIG`、`FLUSHALL`、`FLUSHDB`、`MODULE`、`REPLICAOF`、`SHUTDOWN` 及 `CLIENT PAUSE/KILL/UNBLOCK` 等管理命令；broker 只开放 Celery/redis-py 建连必需的 `CLIENT ID/GETNAME/SETNAME/SETINFO/GETREDIR` 子命令。管理账号不进入应用 Compose；托管平台管理操作只能走受控运维身份和双人审批。

基础 Compose 中的三个单节点服务仍只用于 development/test 和镜像契约验证；生产
`isolated-standalone` 必须由正式入口叠加专用 TLS/持久化合同，不能只启动基础 Compose。
生产应用连接由 `REDIS_BROKER_HOST`、`REDIS_AUTH_HOST`、`REDIS_CONTROL_HOST` 指向三个
独立端点，密码仅由三个同名 Docker secrets 提供；禁止配置 `REDIS_URL` 或把 URI/密码写入
`.env`、Compose environment、日志、镜像层和工单。生产运行时强制生成 `rediss://` 连接，
并从 `REDIS_CA_CERTS_FILE` 读取可信 CA，对 redis-py、Celery broker 和 result backend 同时
启用证书链与主机名校验；CA 不可读时启动失败。私有 CA 必须以只读文件挂载到所有 Redis
消费容器的同一容器内路径。

## 托管服务基线

- 每个域至少跨两个故障区，启用自动主从切换；维护窗前验证客户端 DNS/endpoint 在切换后保持稳定。
- auth/control 启用 AOF `everysec` 或托管服务等价持久化、加密快照和跨区域副本；broker 按 Outbox 可重放边界设置同等级或更强持久化。
- `maxmemory-policy=noeviction`；达到容量阈值时明确失败并告警，不得静默驱逐会话撤销、限流或配额键。
- 禁止公网入口；安全组只允许对应应用子网。传输层使用托管服务 TLS 与 CA 校验，证书到期在 30/14/7 天告警。
- 连接预算按 API/worker/beat/outbox 进程数核算，至少保留 20% 管理与切换余量。

## 监控与阻断阈值

### Phase 0 `isolated-standalone`

三个域分别采集可达性、连接数/拒绝连接、命令延迟 p95/p99、blocked clients、内存占用、
eviction、AOF rewrite/last write 错误、TLS 证书剩余天数和按域认证失败。任何域不得使用手机号、
用户名、batch_no 或异常原文作为标签。此模式没有 replica，不得伪造 replication lag、主从切换
次数或 Redis 备份年龄指标：

- 任一域不可达、eviction 非零、AOF 最近写失败或 TLS 过期/校验失败：critical；
- 连接或内存使用率超过 80%、p99 超过容量基线：warning，持续两个窗口升级 critical；
- broker 故障与 PostgreSQL Outbox backlog/dead 联判；auth 故障与 fail-closed 计数联判；control
  故障同时核对 PostgreSQL usage ledger 与 projection drift；
- 同 VM 采集不足以发现整机失联，必须另有 VMware/宿主外心跳告警并送达企微和公司邮件。

### `managed`（未来模式）

除上述业务指标外，三个独立托管端点还须分别采集复制状态、replication lag、主从切换次数和
托管快照年龄。复制中断为 critical；replication lag 超过内部 RPO 为 warning，持续两个窗口
升级 critical。只有提供商读回的三个实例/复制组 ID 和切换证据才可启用这些指标或声称 HA。

## 切换与恢复演练

### Phase 0 整 VM 恢复

1. 记录候选 commit、三个容器/ACL generation、TLS 证书代次、PostgreSQL 恢复点和演练人/复核人，
   不记录 secret、手机号或 Redis key。
2. 在隔离环境模拟整台生产 VM 丢失，而非只重启某一 Redis；从获批镜像、snapshot 绑定的
   recovery-crypto/backup-passphrase generation 和恢复时当前获批的 Redis/DB 等运行凭据建立
   三套空 Redis AOF 目录，并从 PostgreSQL 备份恢复事实源；不得复活整套已吊销旧凭据。
3. 验证 auth 在 Redis 不可用和新建期间保持 fail closed；broker 不丢 PostgreSQL Outbox，恢复后
   按稳定 event ID 回放；control 仅从 usage ledger 重建绝对投影且 drift 回到零。
4. 复核 TLS 主机名、关闭 default 用户、三域 ACL/key 前缀、危险命令拒绝、AOF 重启保留和
   `noeviction`，再记录平台数据 RPO、`platform_recovery_elapsed`、连接峰值、p99、blocked
   clients、eviction、Outbox 和 projection 差异。RPO>24h 或任一 fail-closed 失效即演练失败；
   该恢复耗时是业务回退 RTO 的输入和独立指标，不得冒充 business-RTO。

### `managed` 主从切换（未来模式）

1. 记录三个实例/复制组 ID、ACL 版本、托管快照对象、候选 commit 和双人复核，不记录 secret。
2. 分域制造主节点故障，确认稳定 endpoint 完成切换；并发验证 JWT 撤销、Outbox 发布、配额预留
   与回补，记录 replication lag、连接峰值、p99、blocked clients 和切换次数。
3. 从随机托管快照恢复到隔离实例，验证 ACL、default 用户、复制状态、key domain 和最小命令集。

候选镜像必须运行 `scripts/verify_redis_domains.sh`，实际验证三域凭据互不通用、跨域 key/危险命令被 ACL 拒绝、default 用户关闭及 AOF 重启保留数据。该脚本不能证明三域位于不同 VM、具备复制或能够承受共同宿主故障。托管服务的监控建设与主从/跨区切换验收不纳入 Phase 0；若未来切换到 `managed`，须按本节步骤完成独立变更并留存外部证据。Phase 0 的整 VM 恢复和三域隔离证据仍是生产首发门禁。

## 密码轮换

三个 ACL 密码独立轮换，禁止复用或跨域回退。

- `isolated-standalone`：取得维护窗口，保留旧 canonical generation，先在权威目录以同目录临时
  文件和 `rename` 替换对应 `0600` secret，再执行 `sms-compose rotate backend`，让目标 Redis
  ACL 与全部获准客户端在同一受控动作中重建。三域互斥登录、健康、Outbox、auth fail-closed 和
  control drift 探针全部通过后才能结束维护窗；失败时恢复同一域旧 generation 并重建，禁止
  用其他域密码临时顶替。当前没有在线双密码承诺，轮换允许在 12 小时 RTO 窗口内短暂停服。
- `managed`：先在托管平台创建同域新凭据并复核最小 ACL，再原子替换权威文件和重建客户端；
  健康及业务探针通过后吊销旧凭据。提供商不支持重叠凭据时必须采用获批停机方案。

## 降级与回滚

- auth：保持 fail closed；不得切到 control/broker，也不得绕过撤销检查。
- broker：dispatcher 持续把失败写回 PostgreSQL Outbox 重试状态；恢复后按稳定 event ID 发布。
- control：暂停需要新配额/频控判断的写路径；恢复后从 PostgreSQL usage ledger 重建并核对 drift。
- 代码回滚不得合并 Redis 域、重新启用 default 用户或恢复 `REDIS_URL`。若托管端点回滚，仍需三个独立实例和三个独立 secrets。
