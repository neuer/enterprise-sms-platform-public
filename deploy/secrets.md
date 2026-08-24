# 生产运行 secrets 手册

## 硬性边界

所有运行凭据只允许以 `deploy/secrets/<name>` 宿主文件作为权威源。生产目录必须是非符号链接目录、mode 精确为 `0700`，并且恰好包含下表 25 个非空普通文件、mode 精确为 `0600`；不得出现 `dev-apikeys.txt`、额外文件或符号链接。不得把值写入 `.env`、数据库、Compose environment、镜像、日志、API、前端、工单或聊天。

生产根目录 `.env` 必须显式且唯一设置 `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`，不得通过 shell 或 `.env` 激活任何 `COMPOSE_PROFILES`/`dev` profile。production 的启动与轮换会 fail-closed 校验这些明确的非密钥键。唯一生产 Compose 入口是 `sudo /usr/local/sbin/sms-compose ...`；动作必须是第一个参数，包装器拒绝 `--profile`、额外 `--env-file` 等全局参数作为首参，固定注入项目根 `.env` 与 Compose 文件，禁止绕过包装器直接运行 Compose。production `up` 只允许文档化的安全选项和固定生产服务，拒绝 `mock-vendor`、未知服务、`--scale`、构建、拉取及其他参数；development Mock 路径不受此生产参数白名单限制。

所有 `up/down/run --rm migrate/rotate backend` 通过 `run_with_lifecycle_lock.py` 使用同一个非阻塞 Python `fcntl.flock`。helper 通过 `Popen(..., pass_fds=...)` 让固定私有动作继承锁 FD；wrapper 在任何预处理器/Docker 动作前调用 `verify-held`，先以只读 `fstat` 核对 mode、euid、常规文件与 dev/ino，再以独立 `O_NOFOLLOW` probe 确认 inode 已有锁，最后对传入 FD 本身执行幂等 `LOCK_EX|LOCK_NB`。只有继承的同一 open-file-description 能成功；另一个 holder 持锁时，同 inode 未锁 FD 会失败。单独伪造 marker、无锁 FD 或错误 inode 均无法 dispatch。helper 收到 TERM/INT/HUP 时转发给子进程并继续 wait；遭 SIGKILL 时，继承锁 FD 仍由阻塞中的子进程/后代持有，到最后一个持有者退出才由内核释放。锁覆盖 prepare、启动/停止、残留确认、cleanup 或轮换恢复。`SMS_RUNTIME_ROOT` 必须是无 `..` 的绝对路径，词法规范化去除尾斜杠后派生 `${SMS_RUNTIME_ROOT}.lifecycle.lock`。首次启动/reboot 即使最终锁父目录缺失也会以当前 euid、安全创建为 `0700` 非符号链接目录；锁文件为 `0600` 常规文件并使用 `O_NOFOLLOW`。运行密钥 cleanup 不删除该锁 inode，`config`、`ps`、`logs`、`exec` 入口不取得该锁。

统一发布的 `release prepare` 只验证封闭发布包并记录基线，不准备新 generation，也不取得锁，必须与 systemd/包装器生命周期动作错开。`release activate/resume/rollback` 在调用发布状态机前执行运行密钥 prepare，并与 up/down/rotate/migrate 共用**同一个 lifecycle flock**；release manager 通过 `exec` 成为锁 helper 的直接子进程，因此 TERM/INT/HUP 能写入中断检查点后再释放锁。整个过程中只允许记录 generation 路径和状态；运行密钥仍遵守**不打印值、长度、摘要或哈希**的边界。`release status` 只读，不创建运行密钥副本、不取锁。

## 权威源与运行副本

`deploy/scripts/prepare_runtime_secrets.py` 在每次受控启动前校验权威清单，并在 tmpfs 路径 `/run/sms-platform/secrets` 创建不可变 generation。`/run/sms-platform/secrets/current` 是指向完整 generation 的原子相对符号链接；每个运行副本都是普通文件、mode 精确为 `0400`，值、长度、摘要和哈希均不得出现在输出中。

| 运行目录 | 文件 owner | 精确内容 | Compose 消费者 |
|---|---:|---|---|
| `current/backend/` | UID 10001 | 厂商两项、AES/HMAC、主体与 API/realtime/bulk 四个审计 HMAC、企微公私钥对、JWT、LDAP、metrics 抓取 token、七个 `db_<role>_password`、三个 Redis ACL 密码 | 由 Compose 按职责最小挂载；企微私钥只挂 callback，自治审计 key 只挂对应生产者域 |
| `current/postgres/` | UID 70 | `db_owner_password`、七个 `db_<role>_password` | postgres 只消费 owner；db-role-provision 消费全部 DB 密码 |
| `current/migrate/` | UID 10001 | `db_owner_password`、`audit_context_key`、三个 `audit_system_<domain>_context_key` | migrate 将四个审计 key 写入 owner-only 验证表 |
| `current/redis/` | UID 999/GID 1000 | 三个 Redis ACL 密码、Redis TLS 服务端私钥 | redis broker、redis-auth、redis-control；TLS 私钥绝不复制到 backend |

generation 与四个服务目录均只允许 root 遍历。Compose 的 source 可以使用内部别名，但容器内 target 始终保持 `/run/secrets/<权威名称>`；运行态后端绝不能看到 `db_owner_password`，API 绝不能看到 broker 密码，worker-callback 绝不能看到 auth 密码。旧 generation 至少保留到新容器健康确认，只有受控清理才可删除。

## 25 件清单与挂载矩阵

| secret 名 | 内容格式与生产来源 | Compose 挂载服务 | 轮换后动作 |
|---|---|---|---|
| `vendor_secret_name` | 厂商控制台签发的非空文本；从受控密钥系统下发 | worker-realtime、worker-bulk | 与 SecretKey 同窗原子替换，滚动重启使用方，先 GetBalance 验证 |
| `vendor_secret_key` | 厂商 SecretKey 非空文本；禁止进入厂商报备工单 | worker-realtime、worker-bulk | 同上；确认旧直连系统已停用后再吊销旧值 |
| `data_aes_key` | 32 随机字节的 base64，或 README 规定的 AES keyring JSON | api、worker-realtime、worker-bulk、worker-callback | 与 HMAC keyring 版本集合一致；保留仍被 `key_version` 引用的旧版本 |
| `data_hmac_key` | 独立 32 随机字节的 base64，或 HMAC keyring JSON | api、worker-realtime、worker-bulk、worker-callback | 与 AES 同窗更新；先验证历史 HMAC 查询再切 active_version |
| `audit_context_key` | 稳定 human/api_app 主体专用 32 随机字节 base64；不得复用其他 key | 仅 api、migrate | 与自治事件 key 同窗轮换；先运行 migrate 同步 owner-only 验证表，再重建 api，禁止挂载到 worker/beat/outbox |
| `audit_system_api_context_key` | API 自治事件专用 32 随机字节 base64 | 仅 api、migrate | 只签 `AUDIT_PRODUCER_DOMAIN=api`，不得复用任何审计 key |
| `audit_system_realtime_context_key` | realtime worker 自治事件专用 32 随机字节 base64 | 仅 worker-realtime、migrate | 只签 realtime 域；不得挂给其他 worker |
| `audit_system_bulk_context_key` | bulk worker 自治事件专用 32 随机字节 base64 | 仅 worker-bulk、migrate | 只签 bulk 域；不得挂给其他 worker |
| `alert_credential_public_key` | X25519 原始 32 字节公钥的 base64；必须与私钥配对 | 仅 api | 与私钥同窗轮换；旧企微配置会清空，轮换后由管理员重新配置并测试 |
| `alert_credential_private_key` | X25519 原始 32 字节私钥的 base64；仅 callback 可解封 | 仅 worker-callback | 与公钥同窗轮换并重建 callback；绝不挂载 api 或其他 worker |
| `jwt_secret` | 裸 v1 key 或版本化 JSON keyring（`active_version` + base64 keys）；由内部密钥系统生成 | 仅 api | 新签发令牌带 `kid/iss/aud`；`JWT_ACCEPT_LEGACY=true` 观察窗口仅允许非生产迁移期，**生产必须为 false**，关闭后必须完成 keyring 迁移 |
| `ldap_bind_password` | 专用最小权限 AD bind 账号密码 | 仅 api | 先在 AD 更新，再原子替换文件并重启 api；用四角色登录验证 |
| `metrics_scrape_token` | 至少 48 随机字节的独立抓取凭据 | 仅 api；Prometheus 使用仓库外只读副本 | 原子替换两端文件并重启 api；旧值立即失效 |
| `db_owner_password` | 独立高熵 PostgreSQL owner 密码 | **仅 postgres、db-role-provision、migrate** | 按 dba.md 维护窗流程更新；严禁挂载 api/worker/outbox-dispatcher/beat |
| `db_auth_password` | 独立高熵 auth 角色密码 | db-role-provision、api | 重跑 provision 并重建 api |
| `db_accept_password` | 独立高熵 accept 角色密码 | db-role-provision、api | 重跑 provision 并重建 api |
| `db_send_password` | 独立高熵 send 角色密码 | db-role-provision、realtime/bulk worker | 重跑 provision 并重建两个发送 worker |
| `db_callback_password` | 独立高熵 callback 角色密码 | db-role-provision、api、realtime/callback worker | 重跑 provision 并重建使用方 |
| `db_export_password` | 独立高熵 export 角色密码 | db-role-provision、api、bulk worker | 重跑 provision 并重建使用方 |
| `db_scheduler_password` | 独立高熵 scheduler 角色密码 | db-role-provision、beat、outbox-dispatcher | 重跑 provision 并重建使用方 |
| `db_metrics_password` | 独立高熵只读 metrics 角色密码 | db-role-provision、api | 重跑 provision 并重建 api |
| `redis_broker_password` | 独立 32–128 字符高熵 ACL 密码 | redis broker、worker、beat、outbox-dispatcher | 原子替换后用 `rotate backend` 同窗重建 broker 与消费者 |
| `redis_auth_password` | 与 broker/control 不同的高熵 ACL 密码 | redis-auth、仅 api | 原子替换后重建 redis-auth 与 api；旧密码不得回退到其他域 |
| `redis_control_password` | 与 broker/auth 不同的高熵 ACL 密码 | redis-control、api、worker、beat | 原子替换后重建 redis-control 与消费者；从 PostgreSQL 事实重建投影 |
| `redis_tls_server_key` | 内部 PKI 为 `redis`、`redis-auth`、`redis-control` 三个 SAN 签发证书所对应的无口令 PKCS#8 PEM 私钥 | 仅三个 Redis 服务端；客户端只挂 CA | 与 `/etc/sms-platform/redis-tls/server.pem` 同窗更新；先在预生产验证证书链、三个主机名和私钥配对，再重建三个 Redis，绝不复制到 backend runtime 目录 |

Compose 后端服务按职责最小挂载，精确矩阵如下；公共 anchor 只保存非敏感运行配置，不得重新加入共享 secrets。

| 服务 | 容器内 secrets |
|---|---|
| api | AES/HMAC、`audit_context_key`、`audit_system_api_context_key`、`alert_credential_public_key`、JWT、LDAP、`metrics_scrape_token`、`db_auth_password`、`db_accept_password`、`db_callback_password`、`db_export_password`、`db_metrics_password`、`redis_auth_password`、`redis_control_password`；不得挂载厂商凭据 |
| worker-realtime | 厂商两项、AES/HMAC、`audit_system_realtime_context_key`、`db_send_password`、`db_callback_password`、`redis_broker_password`、`redis_control_password` |
| worker-bulk | 厂商两项、AES/HMAC、`audit_system_bulk_context_key`、`db_send_password`、`db_export_password`、`redis_broker_password`、`redis_control_password` |
| worker-callback | AES/HMAC、`alert_credential_private_key`、`db_callback_password`、`redis_broker_password`、`redis_control_password` |
| outbox-dispatcher | `db_scheduler_password`、`redis_broker_password` |
| beat | `db_scheduler_password`、`redis_broker_password`、`redis_control_password` |

所有运行态后端仍不得挂载 `db_owner_password`；`mock-vendor` 不挂载任何生产 secret。
数据库身份固定为迁移/恢复使用的 `sms_owner` 与七个职责角色；不得共享密码或恢复旧 `sms_app`。

## 安全落盘

生产值由安全管理员从密钥系统导出到仓库外 `0600` 暂存路径，再使用 `install` 原子设置目标权限。下列命令只处理路径，不打印值、长度、摘要或哈希：

```bash
install -d -m 0700 deploy/secrets
umask 077
install -m 0600 /secure/staging/vendor_secret_name deploy/secrets/vendor_secret_name
# 其余 24 项逐一 install；完成后立即安全删除仓库外暂存副本
```

禁止使用 `echo "$SECRET"`、命令行参数、shell history 或剪贴板落盘。两个数据密钥必须独立生成，禁止复用；八个数据库密码必须全部不同。八个数据库密码和三个 Redis ACL 密码都必须是**无换行单行**，并逐字匹配 `[A-Za-z0-9_+/=-]{32,128}`；禁止空格、引号、反斜杠、控制字符和 shell 片段。运行密钥预处理器会对两个职责组分别执行两两不等校验。

## 恢复密码学代次与备份口令

备份口令不属于 25 件运行 secrets，固定存放在
`/etc/sms-platform/backup-secrets/sms-backup-passphrase`（父目录 `root:root 0700`、文件
`root:root 0600`）。另有两个**不含 secret 值**、但同样按 `root:root 0600` 防篡改的单行 ID：

- `/etc/sms-platform/recovery-crypto-generation-id`：绑定 `data_aes_key`、`data_hmac_key`、主体与
  三个自治审计 keyring，以及需要读取历史告警配置时的 alert X25519 keypair；
- `/etc/sms-platform/backup-secrets/generation-id`：绑定备份口令代次。

每个 snapshot manifest 必须同时记录这两个 ID，不记录值、长度、摘要或派生信息。对应的
recovery-crypto bundle 和备份口令由生产主机之外的受控保管介质按 ID escrow，并在预生产做
取回演练。只要 35 天保留期内仍有 snapshot 引用某 ID，就不得销毁该代次；若轮换，旧代次须
保留到所有引用快照过期并完成销毁读回。灾后恢复只取回 manifest 绑定的上述密码学材料；厂商、
DB/Redis 密码、JWT、LDAP、metrics 与 Redis TLS 使用恢复时当前获批 generation，禁止复活整套
已吊销的旧 25 件凭据。旧 JWT 会话一律失效，告警/外部凭据按变更单重新验收。

## 上线前只读检查

以下检查只显示文件名、mode、UID/GID 与可读性，严禁 `cat`、`head`、字节计数、摘要/哈希、`docker inspect` 环境值或开启 shell trace：

```bash
find deploy/secrets -maxdepth 1 -type f ! -perm 0600 -print
sudo /usr/local/sbin/sms-compose config --quiet
```

第一条必须无输出，`config --quiet` 必须退出 0。再用 `stat` 点名核对权威源
`0700/0600` 与 `current` 各目录的 owner/`0400`，不得用会打印内容、长度、摘要或
哈希的校验方式。生产通用 `sms-compose exec` 已失败关闭，不得为手工查看容器密钥
挂载而放开；最小挂载矩阵由同 commit 的 Compose 展开/契约测试、运行密钥预处理器
与正式 release/bootstrap 证据共同验收。

## 轮换通用流程

1. 创建变更单，写 secret 名、影响服务、窗口与回退人，不写 secret 值。
2. 对需要先改上游的凭据（DB、LDAP、厂商）先完成双值/维护窗协调。
3. 以 `0600` 临时文件写新值，`mv` 原子替换权威源；不修改 Compose secret 名。数据库密码必须先按 `dba.md` 修改数据库角色，再替换权威源文件。
4. 非数据库、非 Redis TLS 的后端凭据执行 `sudo /usr/local/sbin/sms-compose rotate backend`；包装器在共享 lifecycle flock 内覆盖完整轮换，记录严格校验的旧 generation metadata，准备新 generation，并以固定 120 秒上限强制重建和等待全部后端服务。新服务失败时会原子回切旧 generation、再次强制重建并等待旧服务恢复，最终仍失败退出；恢复失败会明确报错。成功和失败路径都保留旧 generation，因为 PostgreSQL 未重建且仍可能引用它。数据库凭据严格使用 `dba.md` 的受控服务集合。`rotate backend` 必须拒绝 CA、server certificate 或 `redis_tls_server_key` 任一变化；TLS 轮换只允许按 `redis-ha.md` 停全栈、整套替换/预检、失败整套恢复，禁止依赖只回退私钥 generation 的自动恢复。成功后检查 `/livez`、`/readyz`、`/metrics`、登录/厂商/数据库只读探针与结构化日志。
5. 确认新值稳定后按上游流程吊销旧值；数据 keyring 旧版本须待重加密完成后才能删除。
6. 归档时间、执行人和验证结果，不归档内容；包装器失败恢复完成后仍须按变更单恢复旧权威文件，并再次验证同一服务集合。只有全栈停止或全部容器重建，并确认 `docker compose ps --all -q` 无容器且无挂载引用后，才允许受控清理旧 generation；不得因代码回退删除数据库卷、权威运行 secret 或仍被容器使用的 generation。
