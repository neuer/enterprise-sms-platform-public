# 生产部署索引

API 容器固定运行两个 Uvicorn worker，以保留强制性能门禁所需的并发余量。两个 worker 都在 API lifespan 内启动任务心跳服务，但 PostgreSQL 会话级 advisory lock 只允许一个进程执行巡检；领导进程退出或数据库连接中断后锁自动释放，存活进程在下一轮接管。不得把该巡检迁移为 beat 任务，也不得通过降低性能阈值替代容量基线。

`deploy/docker-compose.yml` 是服务名、队列名、volume 与 18 件运行 secrets 名称的唯一部署契约。生产变更必须先在同版本冷备或隔离环境执行；不要在生产主机直接试改 Compose。生产唯一入口为 `sudo /usr/local/sbin/sms-compose ...`，它始终显式读取项目根 `.env`、先准备运行密钥并 fail-closed 校验 Compose。

## 权威手册

- [secrets.md](secrets.md)：生产 18 件权威源 `0700/0600`、UID 专属 `0400` 运行副本、挂载矩阵与轮换检查。
- [database-roles.md](database-roles.md)：七个运行职责、显式授权与 fail-closed 回滚。
- [dba.md](dba.md)：`sms_owner` 边界、审计不可变与分区生命周期。
- [backup-restore.md](backup-restore.md)：无明文落盘的加密备份、校验与隔离恢复。
- [vendor-egress.md](vendor-egress.md)：厂商主/备出口 IP 双报备、QPS 与错误码 1010 验证。
- [controlled-real-vendor-test.md](../docs/runbooks/controlled-real-vendor-test.md)：开发测试环境使用正式 Key 的受控真实联调、100 计费条上限与停发规则。
- [test-fast-update.md](../docs/runbooks/test-fast-update.md)：真实联调期间的安全快速更新范围、固定命令和失败关闭流程。
- [usage-ledger-recovery.md](../docs/runbooks/usage-ledger-recovery.md)：配额/频控事实解释、Redis 投影漂移复核与安全重建。
- [redis-ha.md](redis-ha.md)：broker/auth/control 故障域、ACL、托管高可用、切换恢复和密码轮换。
- [database-pool-recovery.md](../docs/runbooks/database-pool-recovery.md)：分进程连接预算、指标、故障恢复与 24 小时混合负载留证。
- [prometheus.example.yml](prometheus.example.yml)：内网 Prometheus scrape 样例。
- [failover.md](failover.md)：T4.9a 冷备同步与 RTO≤30min 切换手册。
- `seed.example.sql`：真实 LDAP 模式 role_mapping 示例；生产禁止执行 `seed-dev`。

上线切换业务顺序以 **PRD.md 第 10 章**为准，需人工签收事项以 **BOOTSTRAP.md 第 8 节**为准。两者与本目录文档冲突时以 PRD 为准并记录变更单。

## 公网端口

- Web HTTP 默认使用宿主机 `18080`，映射到容器内非特权 Nginx `8080`；需要隔离测试时可通过 `WEB_PORT` 覆盖宿主端口。
- `18443` 预留给配置证书后的 TLS 终结服务；未配置证书前不得作为 HTTPS 对外宣称可用。
- API `8000` 与 dev profile 的 Mock `9028` 只绑定宿主机回环地址，不得直接暴露到公网。
- 云主机防火墙与 Docker `DOCKER-USER` 转发链必须阻断 `80/443/8080/8443/8000/9028` 的公网入站，只允许经审批的 `18080/18443`。

## 存活、就绪与容器运行边界

API 的 `/livez` 只证明事件循环仍可响应，不访问数据库、Redis、密钥或运行配置；进程管理器
可用它判断是否需要重启。`/readyz` 是唯一接流判据，在一个 2 秒总预算内串行验证必要
secrets 可读取和解析、数据库迁移为镜像唯一 head、关键运行配置完整有效、当前月及未来至少
三个月分区齐备、auth/control 两个 Redis 故障域可用，以及启动期调度配置已成功加载。探针排队超过 100ms 或并发
超过 2 个时立即返回 503，防止依赖故障时由探针制造连接风暴。两个响应都固定为最小 JSON，
带 `Cache-Control: no-store`，不会返回组件名、地址、异常、版本或凭据派生信息。

Compose 的 API 健康检查每 10 秒执行一次 `/readyz`，单次上限 3 秒，启动宽限 30 秒，连续
6 次失败才标记 unhealthy；`/livez` 不应作为负载均衡接流条件。依赖短暂失败期间 livez
应继续为 200，readyz 为 503；依赖恢复且迁移、配置与分区再次满足后，readyz 自动恢复为
200，无需重启 API。滚动发布必须先等待新实例 readyz，再移出旧实例；若 livez 失败则由
容器生命周期重启，若仅 readyz 失败则保持实例隔离并调查依赖，不得以放宽超时或跳过检查
强行接流。`/healthz` 仅为旧监控兼容别名，禁止新增部署继续使用。

所有长期运行容器固定显式非 root UID、`read_only` 根文件系统、`cap_drop: ALL`、
`no-new-privileges`、进程/内存/CPU 上限和仅覆盖必要路径的 `tmpfs`；PostgreSQL 与 Redis
只把数据目录写入命名 volume，Web 仅监听非特权 8080。任何新增服务必须继承同等边界并在
Compose 合同测试中逐项登记，不能通过恢复 root、可写根目录或新增 Linux capability
解决启动问题。

## 单前端入口与回退

Web 镜像只包含青鸾单一 SPA：

- 唯一入口：`/`；
- 同源 API：`/api/`；
- 退役入口：`/next` 与 `/next/` 固定返回 `410 Gone`。

所有 history 业务路由必须可直接刷新。Web 健康检查只探测根入口，发布验收覆盖青鸾版登录、首次改密、业务深路由刷新、角色显示和退出，并确认退役入口不再返回 SPA。

单前端回退以整个上一版 Web 镜像为最小单位，沿用“四镜像统一发布”的受控状态机执行。不得只替换 Nginx 配置或手工复制静态文件；源码、构建产物、Nginx 与健康检查必须随同一镜像原子切换。

## 外部 TLS 与浏览器安全策略

容器内 Nginx 只监听 HTTP，不得在非 HTTPS 响应伪造 HSTS。外部 TLS 终结器必须负责证书、HTTP 到 HTTPS 重定向，并在最终 HTTPS 响应返回 `Strict-Transport-Security`；策略至少为 `max-age>=31536000` 且包含 `includeSubDomains`。生产发布前设置真实 `WEB_HTTP_BASE_URL` 与 `WEB_BASE_URL`，执行无凭据探针：

```bash
python3 scripts/verify_web_transport.py \
  --http-base "$WEB_HTTP_BASE_URL" \
  --https-base "$WEB_BASE_URL" \
  --min-certificate-days 14
```

探针要求 HTTP 第一跳只能重定向到配置的 HTTPS origin，TLS 不低于 1.2，证书剩余时间不少于 14 天，并检查 HSTS、CSP 与基础浏览器安全头；应由外部监控至少每日执行，失败立即告警。还必须在唯一入口 `/` 用真实浏览器验收登录、首次改密、注销、超时和多标签页强制下线，记录核心页面无 CSP console violation，且 Application 存储中不存在改密或 step-up 令牌。

内部 HTTP Nginx 的 CSP 禁止内联脚本、脚本属性和内联 style 块，仅为 Element Plus/ECharts 动态布局暂时保留 `style-src-attr 'unsafe-inline'`；这不允许 `<style>` 块或脚本执行。当前所有 JavaScript 均为同源静态文件，因此 nonce/hash 不增加有效覆盖；动态 style 属性无法用固定 hash 完整覆盖。代码未使用 `v-html`、`innerHTML`、`eval` 等 HTML 字符串注入点，Trusted Types 已评估但暂不强制，后续须先在青鸾单一前端以 report-only 验证，再作为独立变更进入 enforcement。内部 Nginx 负责返回 CSP、Permissions-Policy、X-Content-Type-Options、X-Frame-Options 和 Referrer-Policy；外部代理不得删除、重复或放宽这些响应头。

## 厂商与告警出站边界

生产 `VENDOR_BASE_URL` 必须使用 HTTPS；应用的厂商客户端和企微客户端均不继承宿主 `HTTP_PROXY`/`HTTPS_PROXY`，防止携带凭据或业务内容的请求被环境代理改道。DEBUG Mock 环境仍可使用 Compose 内部 HTTP 厂商地址。环境模式、认证路由、验证责任与回滚边界见 [runtime-security.md](runtime-security.md)。

企微告警只接受 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` 官方机器人地址。SMTP relay 的精确允许列表由根 `.env` 的非敏感变量 `ALERT_SMTP_ALLOWED_HOSTS` 提供，例如 `smtp,mail-relay.internal`；管理员页面中的 `alert_smtp_host` 只能选择该集合内主机，不能扩大部署出站范围。保存配置和实际发送会分别复验；历史非法值只保留 `alert_log` 与安全日志，不发起网络连接。修改允许列表必须走部署变更并重启后端服务，不得把密码、token 或 webhook 写入 `.env`。

## 安装受控包装器与 systemd

生产主机必须以 root 安装 host-only 配置、包装器链接与 unit。`/etc/sms-platform/compose.env` 只能包含示例中的六个路径/模式变量：项目根、secrets mode、运行时 secret 根、厂商凭据根、真实联调状态根和控制 socket 根；不得复制项目根 `.env`，也不得出现 18 件 secret 名或值。

```bash
sudo install -d -m 0700 /etc/sms-platform
sudo install -m 0600 deploy/systemd/compose.env.example /etc/sms-platform/compose.env
sudo ln -sfn /opt/sms-platform/deploy/sms-compose /usr/local/sbin/sms-compose
sudo install -m 0644 deploy/systemd/sms-platform.service \
  /etc/systemd/system/sms-platform.service
sudo install -m 0644 deploy/systemd/vendor-control-agent.service \
  /etc/systemd/system/vendor-control-agent.service
sudo install -m 0600 deploy/lifecycle.server.example.json \
  /etc/sms-platform/lifecycle.json
sudo install -m 0600 deploy/systemd/lifecycle.env.example \
  /etc/sms-platform/lifecycle.env
sudo install -m 0644 \
  deploy/systemd/sms-partition-maintenance.service \
  deploy/systemd/sms-partition-maintenance.timer \
  deploy/systemd/sms-backup.service deploy/systemd/sms-backup.timer \
  deploy/systemd/sms-restore-drill.service deploy/systemd/sms-restore-drill.timer \
  deploy/systemd/sms-lifecycle-status.service deploy/systemd/sms-lifecycle-status.timer \
  /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/sms-platform.service
sudo systemd-analyze verify /etc/systemd/system/vendor-control-agent.service
sudo systemd-analyze verify \
  /etc/systemd/system/sms-partition-maintenance.service \
  /etc/systemd/system/sms-backup.service \
  /etc/systemd/system/sms-restore-drill.service \
  /etc/systemd/system/sms-lifecycle-status.service
sudo systemctl daemon-reload
sudo systemctl enable --now sms-platform.service
sudo systemctl enable --now \
  sms-partition-maintenance.timer sms-backup.timer \
  sms-restore-drill.timer sms-lifecycle-status.timer
sudo systemctl status sms-platform.service
```

安装前人工复核 `/etc/sms-platform/compose.env` 恰好六行有效配置，mode 为 `0600`；只检查变量名、路径与 mode，不读取任何权威文件。备份系统还须事先在仓库外提供
`/run/backup-secrets/sms-backup-passphrase` 的 root-owned 0600 文件；示例 lifecycle 配置
只含路径、保留期和 RPO/RTO 目标，不含口令。首次启动也可显式执行
`sudo /usr/local/sbin/sms-compose up -d --remove-orphans`，但日常主机与 Docker 生命周期由
systemd 管理。包装器要求动作是第一个参数，只允许受控 `up`、`down`、`rotate backend`、
精确 `run --rm migrate`、`partition-maintenance [--dry-run]`、一次性 `init-admin`，以及
不会创建、启动或改变容器生命周期的 `config`、`ps`、`logs`、`exec` 诊断入口；其他生命周期和未知动作全部退出 2。包装器拒绝 `--profile`、第二个 `--env-file` 等 Compose 全局参数作为首参，内部固定 `--env-file <项目根>/.env`。production 的 `up`、`rotate`、`run --rm migrate`、`partition-maintenance`、`init-admin` 会只读校验根 `.env` 的 `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`，并拒绝 shell 或 `.env` 中任何非空 `COMPOSE_PROFILES`。缺失、重复、不安全设置或 Compose 展开失败都会在启动前阻断。

开发测试服务器要启用真实联调控制台时，先安装 `deploy/systemd/compose.vendor-test.env.example` 和同 commit 的固定 agent unit，再由 root 执行 `sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform SMS_SECRETS_MODE=development /usr/local/sbin/sms-compose vendor-test bootstrap`。bootstrap 会在 lifecycle lock 内验证纯 Mock 根配置、固定路径和 unit 后执行 `systemctl enable --now`；不得用手工命令绕过这些检查。随后用 `systemctl is-active` 及 `stat` 只核对 `/run/sms-platform/vendor-control/vendor-control.sock`、`control-state.json` 的 owner/mode/类型。Compose 只把独立 UDS 目录只读挂载给 API 与 realtime worker，不挂载 `/run/sms-platform/secrets` 或 Docker Socket。agent 安装和应用发布都不得自动进入 controlled；正常的凭据、号码、激活、暂停、恢复和单号码 UAT 操作只能在系统配置页完成。页面轮换由 agent 先恢复私有 `rotation-state.json` 或孤儿 pending generation，再在 lifecycle lock 内保留既有暂停、停止发送进程、确认活动计数清零、建立加密 checkpoint、持久化 previous/new/phase、切换 generation、重建后端并用 GetBalance 验证；失败时写入不受 manual pause 遮蔽的独立 critical 层，回切并重建旧 generation，旧运行态再次通过 GetBalance 后才清理事务。marker 缺失时替换首次激活前凭据还必须再次证明根 `.env` 仍是严格纯 Mock，否则 fail closed。数据 HMAC keyring 增加版本后，active 测试号码须在页面重录同一号码刷新索引，否则真实 UAT fail-closed。版本化凭据根目录禁止进入发布、备份或恢复包，具体边界见受控真实联调手册。

agent 修改根 `.env` 和真实联调 marker 时采用同目录原子替换；unit 因此开放这两个父目录，并单独开放固定密文 checkpoint 目录，但必须用更具体的 `ReadOnlyPaths` 保持源码、Git 元数据、`deploy/secrets`、`compose.env`、生产/冷备环境文件、测试主机 marker 和备份配置只读。只开放现有 `.env` 文件会导致 sibling temp 创建失败，直接开放父目录而不保留更具体只读挂载也不符合本部署合同。每次安装或升级 agent unit 都必须先 `systemd-analyze verify`，再检查 service active 且重启计数不增长。

## 开发测试临时 HTTPS 凭据入口

当前测试服务器的普通入口是 HTTP；HTTP 页面不是安全上下文，手机浏览器不会提供
`crypto.subtle`。正式 Key 仍只能在 `/configs`「真实联调」页通过 WebCrypto 密封，因此
HTTP 入口会隐藏当前密码、SecretName、SecretKey 字段并提示操作者：

```text
当前入口不支持正式凭据安全加密。
请在 ChatGPT 中发送“打开正式凭据安全入口”，
然后通过临时 HTTPS 地址重新登录。
```

不得降级为 HTTP 明文提交、纯 JavaScript 替代加密、命令参数、聊天中转或服务器普通表单。
该入口仅限开发测试，使用 Cloudflare Quick Tunnel，无 SLA；随机
`https://<label>.trycloudflare.com` URL 只改变浏览器到现有 Web 的传输路径，不改变
二次认证、seal session、`RSA-OAEP-256+A256GCM` envelope 或
`vendor-control-agent` 安装协议。

### 一次性主机安装

cloudflared 二进制不进入 Git，服务器不自行下载。由 Codex/运维在本地从 Cloudflare 官方
release 获取 `cloudflared-linux-amd64`，上传前核验锁定合同：

```text
version: 2026.7.2
SHA-256: ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd
```

主机还必须有固定 `/usr/bin/python3`（Python 3.11 以上）和该解释器可导入的
`cryptography`。源码不能由客户端随意上传，也不能从尚未更新的工作树直接执行：先让服务器
取得目标 Git object，再由该 object 生成 root-owned 固定 staging。以下命令中的 commit
必须是已审核并已推送目标分支解析出的完整 40 位 SHA：

```bash
TARGET_COMMIT=<40位目标SHA>
SOURCE_ROOT="/var/lib/sms-platform/test-secure-access-bootstrap/source-$TARGET_COMMIT"
git -C /opt/sms-platform fetch --no-tags origin \
  refs/heads/<目标分支>:refs/test-secure-access/bootstrap
test "$(git -C /opt/sms-platform rev-parse \
  refs/test-secure-access/bootstrap^{commit})" = "$TARGET_COMMIT"
git -C /opt/sms-platform cat-file -e "$TARGET_COMMIT^{commit}"
sudo install -d -o root -g root -m 0700 \
  /var/lib/sms-platform/test-secure-access-bootstrap
if sudo test -e "$SOURCE_ROOT"; then
  sudo mv -- "$SOURCE_ROOT" \
    "$SOURCE_ROOT.previous.$(date -u +%Y%m%dT%H%M%SZ)"
fi
sudo install -d -o root -g root -m 0700 "$SOURCE_ROOT"
git -C /opt/sms-platform archive "$TARGET_COMMIT" -- \
  deploy/scripts/install_test_secure_access.py \
  deploy/scripts/test_secure_access_contract.py \
  deploy/scripts/test_secure_access_runtime.py \
  deploy/scripts/test_secure_access_manager.py \
  deploy/scripts/vendor_test_files.py \
  deploy/scripts/check_test_update_migration.py \
  deploy/scripts/run_with_lifecycle_lock.py \
  deploy/scripts/public_cutover_bootstrap.py \
  deploy/scripts/test_update_apply.py \
  deploy/scripts/test_update_backup.py \
  deploy/scripts/test_update_contract.py \
  deploy/scripts/test_update_manager.py \
  deploy/scripts/test_update_promote.py \
  deploy/scripts/test_update_store.py \
  deploy/scripts/test_update_verify.py \
  scripts/check_public_readiness.py \
  scripts/export_public_snapshot.py \
  scripts/verify_public_snapshot_cutover.py \
  deploy/sms-compose \
  deploy/systemd/sms-platform-test-secure-access.service |
  sudo /bin/tar -x -C "$SOURCE_ROOT"
sudo chmod 0644 \
  "$SOURCE_ROOT/deploy/scripts/install_test_secure_access.py" \
  "$SOURCE_ROOT/deploy/scripts/test_secure_access_contract.py" \
  "$SOURCE_ROOT/deploy/scripts/test_secure_access_runtime.py" \
  "$SOURCE_ROOT/deploy/scripts/test_secure_access_manager.py" \
  "$SOURCE_ROOT/deploy/scripts/vendor_test_files.py" \
  "$SOURCE_ROOT/deploy/scripts/check_test_update_migration.py" \
  "$SOURCE_ROOT/deploy/scripts/run_with_lifecycle_lock.py" \
  "$SOURCE_ROOT/deploy/scripts/public_cutover_bootstrap.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_apply.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_backup.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_contract.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_manager.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_promote.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_store.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_verify.py" \
  "$SOURCE_ROOT/scripts/check_public_readiness.py" \
  "$SOURCE_ROOT/scripts/export_public_snapshot.py" \
  "$SOURCE_ROOT/scripts/verify_public_snapshot_cutover.py" \
  "$SOURCE_ROOT/deploy/systemd/sms-platform-test-secure-access.service"
sudo chmod 0755 "$SOURCE_ROOT/deploy/sms-compose"
sudo install -o root -g root -m 0644 \
  /tmp/cloudflared-linux-amd64 \
  /var/lib/sms-platform/test-secure-access-bootstrap/cloudflared-linux-amd64
sudo /usr/bin/env \
  PYTHONNOUSERSITE=1 \
  "PYTHONPATH=$SOURCE_ROOT/deploy/scripts" \
  /usr/bin/python3 \
  "$SOURCE_ROOT/deploy/scripts/install_test_secure_access.py" \
  --cloudflared-file \
    /var/lib/sms-platform/test-secure-access-bootstrap/cloudflared-linux-amd64 \
  --source-root \
    "$SOURCE_ROOT" \
  --source-commit "$TARGET_COMMIT"
sudo systemd-analyze verify \
  /etc/systemd/system/sms-platform-test-secure-access.service
sudo /usr/bin/env \
  SMS_SECRETS_MODE=development \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH=/usr/local/libexec/sms-platform/test-secure-access \
  /usr/bin/python3 \
  /usr/local/libexec/sms-platform/test-secure-access/test_secure_access_manager.py status
```

每次使用新的 commit 专属 source 目录，旧目录只改名保留，不在其上覆盖解包；Git archive
也只枚举 installer 实际允许的固定资产，未枚举文件不会进入启动 `PYTHONPATH`。由于
`git archive` 的 tar 记录可能受 umask 影响解包为 `0664/0775`，必须在安装前把枚举的
Python/unit 文件逐项归一为 `0644`、`sms-compose` 归一为 `0755`，不得通过放宽安装器权限
校验绕过。安装器先把
每个源码资产的 commit 对象类型、tree mode/type 与字节逐一绑定到该 Git commit，全部验真
后才在 root-only 临时目录用
固定 `/usr/bin/python3` 导入完整主机模块闭包；随后验证 binary 的普通文件/非符号链接、
SHA-256、ELF64 amd64、精确版本和 static unit，并原子安装 root-owned
contract/runtime/manager/unit、test-update bootstrap wrapper、独立
`/etc/sms-platform/test-host` 标记与精确 SHA manifest。它不写真实联调激活标记
`/etc/sms-platform/test-environment`。安装后执行 daemon-reload，但不自动启动或 enable
隧道；重装会先 `disable --now` 并尽力 `reset-failed`，再以 unit 必须为
inactive/static 作为硬判据，既清理上次启动失败，也允许正常停止后已卸载的 static unit
返回“未加载”。为让 static unit 的 `DynamicUser` 读取已知的固定资产路径，
安装器只在全部源码验真
后为 `/usr/local` 到资产目录之间安全、root-owned、不可写的目录补 other traverse 位；
例如 `0750` 只变为 `0751`，不会增加目录 read/list 权限。预期 status 为 `inactive`；
成功后才清理上传文件和 staging。锁定版本或任一
host-control 资产变化时必须重新评审并按目标 commit 重装。
首装验证故意直接调用已验真的固定 manager，因为此时服务器旧 checkout 可能还没有
`secure-access` 命令；目标 commit 经快速更新生效后，日常才使用下节的
`/usr/local/sbin/sms-compose secure-access ...`。

同一安装器还负责快速更新密文 checkpoint 的一次性前置条件，并在任何 checkpoint 权威状态修改前非阻塞取得固定 lifecycle lock；锁冲突时不修改 config、key 或 checkpoint 输出目录。配置与密钥均不存在且输出目录不存在或为空时，才在服务器本机用 `os.urandom(32)` 生成 root `0600` 密钥，并把固定配置的原子出现作为最后的 commit marker；后续重装只验证并保留原密钥。唯一崩溃恢复例外是“安全的 key 已落盘、config 尚未提交、输出目录仍为空”，此时只补 config，提交前后都重新绑定同一 key inode。config 提交后的目录 `fsync` 或身份复核若失败，本次仍 fail closed；下一次运行只有在重新验证全部 checkpoint、config/key 内容与 inode，并再次 `fsync` 两个权威文件父目录后才接受该已提交状态。config-only、密钥长度/权限/硬链接异常、中断 checkpoint，或已有 checkpoint 却缺少任一权威文件都立即 fail closed，绝不生成新密钥覆盖历史恢复能力。安装过程和输出不得显示密钥值、长度、摘要或派生信息。

无历史公开快照与旧测试基线没有共同祖先时，不得继续使用日常快速更新，也不得把私有
归档 remote、commit、ref 或 Git pack 导入当前公开工作区。旧的 public-cutover 服务端
兼容资产仅用于保留既有运行态的可恢复性，不构成可执行入口；本地 driver 已明确拒绝
`--public-snapshot-cutover`。

一次性基线迁移必须另立变更单，在不属于公开工作区的隔离临时证据仓库中完成逐文件验真，
并单独评审 PostgreSQL/volume 保留、18 件 secrets、三域 Redis、七职责数据库角色、回退与
证据清理。迁移完成后的服务器 HEAD 和 origin 必须直接绑定公开仓库 commit，且不得把
任何私有 URL、ref、commit 或对象带回公开工作区；随后才恢复
`scripts/test_update.sh plan/apply/status` 日常流程。

### 手机操作者日常流程

操作者只需发送“打开正式凭据安全入口”。Codex 通过既有受控远程入口执行固定命令：

```bash
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access start
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access status
```

成功只返回 `status=ready`、随机 HTTPS URL 和 `expires_at`。同一活动周期重复 start 幂等，
不会创建第二条隧道。新 URL 写入状态后先保留 5 秒 DNS 传播时间，再在最长 60 秒准备窗口
内完成服务器侧 `/login` HTTPS 回探；窗口内始终失败才自动停止并失败关闭，避免首次
NXDOMAIN 负缓存导致可用隧道被误杀。操作者在手机打开该 URL、重新登录、进入 `/configs`，确认浏览器为
`isSecureContext=true` 且 `crypto.subtle` 可用，再亲自输入正式凭据。Codex、自动化和
验收不得读取、代填或提交正式 Key。

static unit 使用非特权 DynamicUser，只连接固定 `http://127.0.0.1:18080`，15 分钟达到
`RuntimeMaxSec` 后自动终止；也可发送“关闭正式凭据安全入口”，由 Codex 执行。stop 对
`reset-failed` 的“unit 未加载”结果只做尽力清理，最终仍必须确认 unit 为
inactive/unknown 才返回成功，避免已成功停止的 static unit 被误报失败：

```bash
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access stop
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access status
```

停止后应为 `inactive`，旧 URL 不可用，无 cloudflared 进程、额外监听或 `/run` 状态残留。
临时 URL 不写数据库、Git、浏览器存储或长期配置。

### 与快速更新和数据的边界

应用代码先提交、推送并通过唯一入口：

```bash
scripts/test_update.sh plan --ref origin/<branch>
scripts/test_update.sh apply --ref origin/<branch>
scripts/test_update.sh status
```

只有远端 `state=verified` 和对应表面验收完成才算更新成功。driver 只构建受影响镜像且
不重复运行测试；普通无迁移更新允许 CI 并行运行，high-risk、迁移或控制面更新必须先
验证目标 commit 的精确 `ci-gate=success`。无迁移更新不创建数据库 checkpoint，切换或验收失败时自动回退上一版应用
镜像；迁移不自动回退 schema。所有 high-risk 更新的
prepare/apply/verify/status 全程使用同一 root-owned 不可变 bootstrap 快照；若快照到目标
commit 间的 host-control 字节未变可复用既有快照，发生变化则必须先按上文重装目标 commit。
无共同历史的服务器基线必须在本入口前按上文单独迁移；driver 不接收私有 Git 证据。
不得 raw Git/Compose 绕过；driver 内部只用受控、摘要校验和可续传的 rsync 传输归档。快速
更新与一次性主机安装相互独立：快速更新不自动启动隧道，
不安装或轮换正式 Key，不登记测试号码，不激活真实联调，
不执行管理员初始化，也不初始化数据库。始终保留 PostgreSQL 数据库、Docker volume、
凭据 generation、测试号码和运行态数据；任何初始化仍须操作者明确确认。

页面 `reset_configuration` 的 root 撤销阶段只调用 development-only、零参数固定操作 `vendor-test reset-runtime`。该操作和 release 共用同一个 lifecycle flock：先切换到固定 revocation tombstone generation，再停止、删除和重建 api、worker-realtime、worker-bulk，容器内无输出探测通过后才清理 stale runtime generations。它不回切旧 generation，不删除 runtime root、PostgreSQL、Docker volume 或非厂商 secret，也不扩大 systemd capability、可写路径、网络族、sudo 或任意命令能力。任何部分失败由同一 journal operation 重放，固定错误不得携带 Key 或 generation 元数据。

production `up` 白名单只接受 `-d`/`--detach`、`--remove-orphans`、`--no-deps`、`--force-recreate`，以及显式服务 `postgres`、`redis`、`migrate`、`api`、`worker-realtime`、`worker-bulk`、`worker-callback`、`outbox-dispatcher`、`beat`、`web`。`mock-vendor`、未知服务、所有 `--scale`、`--profile`、`--env-file`、`--build`、`--pull` 和其他未列出的参数均在 Python/Docker 调用前退出 2；development 仍可使用 Mock 调试参数。所有 `up/down/run --rm migrate/rotate backend` 由 `run_with_lifecycle_lock.py` 使用 Python `fcntl.flock` 共用非阻塞 lifecycle lock。helper 以 `Popen(..., pass_fds=...)` 让受控子 wrapper 继承锁 FD；私有 `__locked` 在 dispatch 前调用 `verify-held` 核对 FD 类型、mode、euid 与 dev/ino，独立 probe 只确认该 inode 已有锁，再对传入 FD 本身执行幂等 `LOCK_EX|LOCK_NB`：只有继承的同一 open-file-description 成功，指向同 inode 但未加锁的 FD 会在另一个 holder 存在时失败。单独伪造 marker 或 FD 均无效。helper 对 TERM/INT/HUP 转发给子进程并继续等待；即使 helper 遭 SIGKILL，继承锁 FD 仍由子进程及其后代持有，到最后一个持有者退出才由内核释放。`SMS_RUNTIME_ROOT` 必须是无 `..` 的绝对路径，词法规范化会去除尾斜杠，因此等价写法使用同一 `${SMS_RUNTIME_ROOT}.lifecycle.lock`。首次启动/reboot 时 helper 安全创建最终锁父目录，要求 mode `0700`、当前 euid、非符号链接；锁文件以 `O_NOFOLLOW` 创建为 `0600` 普通常规文件。诊断命令不取得该锁，runtime cleanup 也不删除锁 inode。

## 首个本地管理员初始化

数据库迁移完成且 `user_account` 仍为空时，在真实交互 TTY 中执行：

```bash
sudo /usr/local/sbin/sms-compose init-admin --show-temporary-password
# 可选：--username root.admin --display-name 平台管理员
```

命令在数据库事务中取得 advisory lock，只允许空系统的首个执行者成功；默认用户名为 `admin`、显示名为“系统管理员”。20 位临时密码由容器内进程生成，事务提交后仅写入当前控制 TTY 一次，不接受 `--password` 或任何外部密码输入，也不写日志、审计载荷或数据库明文字段。Codex 可通过 PTY 执行该受控命令并把当次 TTY 输出转告操作者；不要把密码追加到命令行、脚本或部署证据。初始管理员第一次登录必须修改密码。该流程只使用内置本地认证源，与 AD 配置、LDAP bind 和目录可用性无关；AD 对接应在登录后的系统配置页另行完成草稿、测试和激活。

## AD 认证源配置

AD 默认禁用，初始化本地管理员并完成首次改密后，由管理员进入“系统参数 → 认证源”配置。非敏感 LDAP 参数不再从 `.env` 读取，必须按以下顺序操作：

1. **保存草稿**：填写 LDAP 服务地址、Base DN、Bind DN、用户过滤器、超时和属性映射。任何编辑都会生成新草稿版本，并使旧测试资格失效。
2. **测试连接**：后端使用当前草稿、`ldap_bind_password` Docker secret 和 `LDAP_CA_CERTS_FILE` 执行服务绑定与有界查询；页面只返回安全结果码，不接收或回显 Bind 密码、文件路径或内容。
3. **启用配置**：只有当前草稿版本测试成功后才能激活。启用后登录页才显示 AD；登录请求只调用用户明确选择的认证源，失败时不回退本地账号。
4. **禁用 AD**：立即从登录页移除 AD 入口，但配置与角色映射均保留；再次启用前应按变更后的当前草稿重新测试。

目录组到平台角色的映射也在该区域维护。AD 账号首次成功登录后进入用户台账；登录名若已被本地身份先占用，将拒绝该次 AD 登录并记录来源冲突审计。生产部署仍必须提供 18 件 secrets，其中 LDAP bind 密码固定为 `ldap_bind_password`，CA 为只读受控文件而非 secret；未来 IAM Provider 仅预留扩展能力，本期不配置、不启用。

## 四镜像统一发布

统一发布以 API、Web、PostgreSQL、Redis 四个镜像和一个 Git commit 为不可拆分候选。发布包是一个 `0700` 目录，内部只能有 mode `0600` 的 `manifest.json` 及清单点名的证据/归档文件；不允许额外文件、符号链接、重复 JSON 字段或相对清单路径。`manifest.json` 必须精确记录：`release_id`、40 位 commit、`development|production` mode、四镜像的 ref/ID/changed、迁移 from/target/compatibility，以及证据文件名。`manual` 兼容性、未绑定证据、脏 Git、镜像 ID/平台不符均在容器变更前失败关闭。

### 公开候选与私有仓库认证边界

手机远程 Mac 或其他无桌面会话先执行 `scripts/docker_public.sh doctor`。本地候选构建、
development archive、G2、快速更新和数据镜像验证会自动进入一次性 public Docker
会话：配置为空认证，不读取 macOS Keychain，也不改变 `~/.docker`。自动化不得删除
全局 Docker 配置、不得解锁 Keychain，也不得在 public 会话执行 login、logout、push
或 registry output。

推送私有仓库以及对四个最终 production RepoDigest 做不可变身份复验属于 authenticated 通道，
必须在受控 CI、专用 Linux 发布构建机或操作者已建立认证上下文的交互会话执行，并显式
设置：

```bash
SMS_DOCKER_ACCESS=authenticated \
RELEASE_API_IMAGE="$API_DIGEST_REF" \
RELEASE_WEB_IMAGE="$WEB_DIGEST_REF" \
RELEASE_POSTGRES_IMAGE="$POSTGRES_DIGEST_REF" \
RELEASE_REDIS_IMAGE="$REDIS_DIGEST_REF" \
RELEASE_SOURCE_REPORT=/secure/releases/release-20260715/candidate-build-gate.json \
  bash scripts/verify_release.sh \
  --report /secure/releases/release-20260715/release-gate.json
```

registry 凭据不得进入聊天、命令参数、日志、发布证据或仓库；认证不可用时停止并转到
受控执行面，禁止回退到匿名结果或临时修改全局 Docker 配置。

### 证据生成与两类门禁

正式候选对四镜像执行一次 Trivy，并把成功报告直接写入封闭发布目录；报告路径必须是
绝对路径。只有 PostgreSQL 或 Redis 镜像实际变化时才运行数据镜像门禁：

```bash
install -d -m 0700 \
  /secure/releases/release-20260715 \
  /secure/releases/release-20260715/sboms
bash scripts/verify_release.sh \
  --report /secure/releases/release-20260715/candidate-build-gate.json \
  --sbom-dir /secure/releases/release-20260715/sboms
bash scripts/verify_reproducible_build.sh \
  --baseline /secure/releases/release-20260715/candidate-build-gate.json \
  --report /secure/releases/release-20260715/reproducibility.json \
  --sbom-dir /secure/releases/release-20260715/sboms
POSTGRES_IMAGE="$POSTGRES_DIGEST_REF" REDIS_IMAGE="$REDIS_DIGEST_REF" \
  CANDIDATE_SHA="$CANDIDATE_COMMIT" \
  bash scripts/verify_data_images.sh \
  --report /secure/releases/release-20260715/data-images.json
```

`scripts/verify_release.sh` 会让 Trivy 输出四份机器可读 JSON，并生成去除时间与 UUID、稳定
排序的 CycloneDX，再由 `scripts/render_release_evidence.py` 核对每份报告的镜像 ref、
image ID 与零发现结果；最终 `gate_type=release` 报告为每个镜像记录
`scan_report_sha256`。`verify_reproducible_build.sh` 随后以同一 commit、固定基础镜像
digest、`linux/amd64`、无缓存和 `SOURCE_DATE_EPOCH=0` 独立重建四镜像，只重新生成
CycloneDX，不重复漏洞扫描；四个 image ID 或四份规范 SBOM 摘要任一不一致即失败关闭，
并写 mode `0600` 的 `reproducibility.json`。通用 writer 不再允许只凭 ref/ID 参数重包装
PASS。

本地候选构建报告必须原样保留，作为后续 promotion 的来源证据。推送到受控仓库后，
生产负责人通过 `RELEASE_SOURCE_REPORT` 指向候选报告。脚本以 `linux/amd64` 回拉四个
最终 RepoDigest，要求 image ID 与候选逐一相等，再复用候选的 Trivy 结果生成最终证据；
相同内容不重复扫描。最终报告内嵌来源报告摘要、扫描摘要和四镜像绑定。禁止覆盖候选
报告、手改 `passed` 或把旧报告换名复用：

```bash
RELEASE_API_IMAGE="$API_DIGEST_REF" \
RELEASE_WEB_IMAGE="$WEB_DIGEST_REF" \
RELEASE_POSTGRES_IMAGE="$POSTGRES_DIGEST_REF" \
RELEASE_REDIS_IMAGE="$REDIS_DIGEST_REF" \
RELEASE_SOURCE_REPORT=/secure/releases/release-20260715/candidate-build-gate.json \
SMS_DOCKER_ACCESS=authenticated \
  bash scripts/verify_release.sh \
  --report /secure/releases/release-20260715/release-gate.json
```

环境变量只放非密钥镜像引用。门禁成功后使用生成器从最终报告写
`manifest.json`，不要手填四镜像 ref/ID。以下示例是 API/Web 无迁移发布：

```bash
python3 scripts/create_release_manifest.py \
  --release-report /secure/releases/release-20260715/release-gate.json \
  --output /secure/releases/release-20260715/manifest.json \
  --release-id release-20260715 \
  --migration-from "$MIGRATION_HEAD" \
  --migration-target "$MIGRATION_HEAD" \
  --changed api \
  --changed web
```

数据镜像变化时再追加 `--data-images`；PostgreSQL 变化时还必须追加
`--backup-record` 与 `--restore-report`。生成器会原子写入 `0600` 文件并调用现有
exact-field 契约自校验。最终目录清单必须与 manifest 声明完全相等。

`gate_type=release_control_smoke` 只由 `scripts/verify_release_control.sh` 在隔离 development project 中生成，用于配置失败、健康失败、自动补偿、TERM 中断和续跑的控制面证据。它明确 `scan_performed=false`，不替代 Trivy，不代表发布就绪，生产 mode 和 `scripts/deploy_release_remote.py` 均拒绝该证据。正式发布只能使用 `release_gate_kind=release`。

### development archive 与 production preloaded digest

- `development archive`：每个 changed 镜像必须提供清单点名的 tar 和 SHA-256；prepare 校验后执行受控 `docker load`。未变化镜像的 archive 字段必须为 null。此模式用于隔离 Mock/控制面演练，不得作为生产镜像来源。
- `production preloaded digest`：四个 ref 必须是受控仓库 `image@sha256:RepoDigest`，镜像须在目标主机预先拉取；清单 archive 字段全部为 null，prepare 不拉取网络资源，并逐一核对本地 image ID、`linux/amd64` 与 RepoDigests。PostgreSQL changed 时还必须附加同 commit 的加密备份变更记录和隔离恢复报告。

清单由 `scripts/create_release_manifest.py` 按 `deploy/scripts/release_manifest.py` 的
exact-field 契约自动生成并用 `0600` 原子落盘。Web-only production 清单只把 Web 标为
changed；四个镜像仍全部绑定最终 digest。PostgreSQL/Redis changed 必须点名
`data-images.json`，迁移只允许 `none` 或向后兼容 `expand`。

### 本机状态机命令

以下命令只能在目标主机使用安装好的受控入口；`--manifest` 必须是封闭 staging 目录内的绝对路径：

```bash
sudo /usr/local/sbin/sms-compose release prepare --manifest /secure/staging/release-20260715/manifest.json
sudo /usr/local/sbin/sms-compose release status --release-id release-20260715
sudo /usr/local/sbin/sms-compose release activate --release-id release-20260715
sudo /usr/local/sbin/sms-compose release resume --release-id release-20260715
sudo /usr/local/sbin/sms-compose release rollback --release-id release-20260715
```

`release prepare` 是受控准备动作：验证证据、当前 commit/容器、镜像和迁移基线并写入 `/var/lib/sms-platform/releases/<release_id>`，但不改根 `.env` 或容器，也不取得 lifecycle lock；因此必须在没有 systemd/包装器生命周期操作的准备窗口执行。`release status` 只读且不取锁。`release activate/resume/rollback` 会准备运行密钥，并与 up/down/rotate/migrate 共用同一个 lifecycle flock；锁冲突时在任何 Docker 或状态修改前退出。成功状态重复调用幂等；succeeded 发布不能原地 rollback，回退必须作为绑定旧 digest 的新发布执行。

systemd 不直接执行 release；`RemainAfterExit=yes` 的 unit 只负责开机 `up` 与关机 `down`。unit 保持 active 时执行上述受控发布命令，发布从 prepare 到终态期间不得并发执行 systemctl restart，也不得并发执行 systemctl stop、Docker restart 或其他 `sms-compose` 生命周期动作。`release activate/resume/rollback` 与 unit 的 ExecStart/ExecStop 使用同一个 lifecycle flock，避免容器和运行密钥交叉修改。

### 维护窗口与固定激活顺序

单实例 Compose 不承诺零停机。Web-only 只有 Web 替换时间；API、PostgreSQL 或 Redis changed 必须取得独占维护窗口，暂停流量/发送并通知业务。只执行清单需要的步骤，但全量候选的固定顺序如下，禁止人工换序：

1. `quiesce_backend`：停止 beat、Outbox dispatcher、三个 worker 和 API；
2. `wait_beat_lease`：固定等待 **31 秒**，兼容旧 beat 的 30 秒 Redis 租约；
3. `recreate_postgres`：仅在 PostgreSQL changed 时替换并等待健康；
4. `recreate_redis`：仅在 Redis changed 时替换并等待健康；
5. `run_migrate`：仅在 migration from/target 不同时执行一次 owner 迁移；
6. `recreate_backend`：同一 API 镜像重建 API、三个 worker、Outbox dispatcher、beat；
7. `recreate_web`：仅在 Web changed 时替换；
8. `final_runtime`：绑定四镜像 ref+ID、容器身份/健康、三个 Celery pong、迁移 head 和 tracked-job 运维探针。

### 补偿、续跑和人工停止边界

每一步先写 intent、后写 observation。明确失败会按已完成层反向安全补偿；数据镜像只有在未执行迁移且已证明可回退时才切回。迁移已经执行时，`none/expand` 允许保留兼容 schema，并在状态的 `residual_changes` 明确记录；事件文件、原始 env 与发布包不会因失败被覆盖。TERM/INT/HUP 会在当前外部动作返回后写入中断信号与 `next_step`，再次 `release resume --release-id` 先观察实际 env、容器 ref+ID、健康和数据库 `alembic_version`，再决定继续、回滚或完成。

状态为 `recovery_required` 时表示运行态、迁移态或补偿结果存在歧义；工具拒绝自动 resume 和自动 rollback。此时立即保持维护窗口、停止人工 Compose/SQL 操作，归档 `state.json`、`events.jsonl` 和 `residual_changes`，由 DBA、发布负责人和回退决策人根据数据库/容器只读证据批准新的恢复步骤。禁止猜 migration head、强行 stamp、删除状态目录或重跑迁移。

统一发布明确排除 PostgreSQL/Redis **跨大版本**在线升级和任何 **破坏性迁移**；这两类变更必须使用独立的数据迁移/停机方案、隔离恢复证据与专项变更单，不能伪装为 `expand`。自动流程也不负责 schema downgrade。

### 首次引导、远端执行与保留策略

`首次引导`时，先以既有受控方式把已审核 commit、`deploy/sms-compose`、release scripts、systemd unit 安装到 `/opt/sms-platform`，执行 `systemd-analyze verify` 并确认当前四个容器与根 `.env` 的 ref/ID 一致；再预加载四个 production digest。首次 release 只能从这个已验证基线 prepare，不能用发布工具自举替换它自身。后续才使用统一状态机。

发布前先在隔离远端 Mock 主机用同 commit、独立 Compose project/端口/卷完成 changed-subset 演练，且保留未变化容器 ID 证据。生产联网交接只允许本地 driver 一次执行：

```bash
uv run --project backend python scripts/deploy_release_remote.py \
  --manifest /secure/releases/release-20260715/manifest.json \
  --host sms-prod.example.internal --port 22 --user deploy \
  --platform-root /opt/sms-platform \
  --runtime-root /var/tmp/sms-platform-release-staging \
  --mode production --remote-ref origin/main \
  --public-url https://sms.example.internal/readyz
```

driver 只以固定 argv 执行 Git fast-forward、逐文件哈希上传、prepare/activate/status 和公网探针，不拼接远端 shell。上传前会依次把暂存根和单次发布目录收紧为 `0700`，避免首次递归建目录遗留可遍历的中间目录。每个文件始终写入同名固定 `.part`，`rsync --partial --inplace` 失败时最多自动重试 3 次；因此 macOS `openrsync` 或 SSH 长连接中断后可从同一个远端文件续传。只有远端 SHA-256 与本地清单一致才原子改名，三次仍失败则在 Git、prepare 和 activate 前失败关闭并保留 `.part` 供下一次续传。它会把清单枚举后的 `SMS_SECRETS_MODE=development|production` 通过固定 `/usr/bin/env` argv 显式交给 sudo wrapper，不依赖 sudo 环境继承；服务器 fetch 后还会要求获批 remote ref 精确解析为清单 commit，已存在的 rollback ref 只读复核、绝不 force 覆盖。终态必须同时通过 `systemctl is-active --quiet sms-platform.service` 和 `git status --porcelain --untracked-files=normal` 空输出。

development 的统一高风险发布还必须处理宿主 `vendor-control-agent.service` 的进程版本。driver 以本次持久化 `release-rollback/<release_id>` 为基线，只比较 agent 入口及其固定本地依赖；即使 Git 已在上一次中断中快进到候选，同一 release 重试仍能识别需要重载。只有 release status 已严格到达 `succeeded` 且这些文件确有变化，才以固定 argv 执行 `sudo -- /usr/bin/systemctl restart vendor-control-agent.service`，随后执行 `systemctl is-active --quiet vendor-control-agent.service`。任一 restart/active 校验失败都使 driver 失败关闭，并在公网探针和最终成功确认前停止；不得把旧 agent 进程与新 API/journal 混用。production 发布和普通 `scripts/test_update.sh` 不进入此 development-only 阶段。

公网 URL 参数强制精确指向 `/readyz`，只证明必要运行依赖已满足接流条件，不代表身份链路
成功。远端 Mock 演练和生产变更在 driver 成功后都必须另做**浏览器登录验收**：使用对应
环境的受控测试账号完成登录、首页加载、角色显示和退出；不得把凭据放入 driver 参数、日志
或发布证据。Codex 或其他受限执行器运行该联网 driver 前，操作人必须在**工具沙箱**显式
批准这一次网络调用；仓库代码无法自行授予沙箱或 SSH 权限。先用 `--dry-run` 审核的输出
已经脱敏，但 dry-run 不替代真实执行或浏览器验收。

每次生产执行必须有生产变更单，记录 release_id、commit、四个 RepoDigest/image ID、Trivy/数据/备份恢复证据、changed subset、迁移兼容性、维护窗口、执行人、复核人、回退决策人、开始/结束时间和终态；不记录任何 secret 或手机号。

`/var/lib/sms-platform/releases` 的发布包、状态、事件、原始 env，以及当前与上一成功版本镜像至少保留到观察期结束和变更单关闭。工具**不自动 prune** 发布目录、旧镜像、数据卷或运行密钥 generation。清理必须是单独审批动作，先证明没有容器/回退点引用；不得执行无范围的 `docker image prune`、删除数据库卷或删除 `recovery_required` 证据。

## 生产上线顺序

### 独立发布镜像安全门禁

冻结候选 commit 后，必须在可联网更新漏洞库、可访问 Docker daemon 的受控构建机执行：

```bash
bash scripts/verify_release.sh
```

该门禁独立于 G2：日常 Mock G2 保持确定性且不依赖外网；发布门禁基于 digest 固定的官方 Alpine 基线，真实构建 API、Web、PostgreSQL、Redis 四个最终交付镜像，再使用 digest 固定的 Trivy 0.70.0 扫描实际部署产物。扫描参数固定为 `HIGH/CRITICAL`、`--exit-code 1`，漏洞库更新、Docker、构建、扫描或发现漏洞任一失败都会原样阻断发布，禁止降低阈值或忽略 unfixed 漏洞。公开文档只保留门禁结论，不记录内部候选 SHA、工作流运行号或镜像标识；最近一次已归档验证结果为 API 0、Web 0、PostgreSQL 0、Redis 0，即四个镜像均为 `0 HIGH / 0 CRITICAL`。

脚本要求 Git 工作树（含暂存区与未跟踪文件）干净，打印候选 commit，并使用 Docker volume `trivycache` 复用漏洞库缓存；它不读取生产 secrets。归档材料必须包含候选 commit、完整扫描报告、Trivy 镜像 digest、最终 API/Web/PostgreSQL/Redis 镜像 digest 与执行时间。首次运行或缓存失效会联网下载漏洞库，失败时不得把旧报告冒充本次证据。

四个生产镜像引用由 `.env` 的 `SMS_API_IMAGE`、`SMS_WEB_IMAGE`、`SMS_POSTGRES_IMAGE`、`SMS_REDIS_IMAGE` 提供；本地默认标签只用于构建，生产必须替换为受控仓库的 `image@sha256:RepoDigest`。推送前还要对同一候选的数据镜像执行：

```bash
bash scripts/verify_data_images.sh
```

该脚本使用临时文件型 secrets 和一次性卷，验证 PostgreSQL 七个 NOLOGIN
占位角色的最小属性、初始化与重启持久化，以及 Redis AOF 重启持久化；不得改成明文
密码环境变量。脚本通过不替代备份隔离恢复和 RTO 验证。

1. 冻结发布版本，记录 Git commit、镜像 digest、变更单、执行人和回退决策人。
2. 按 `secrets.md` 从受控密钥系统落盘恰好 18 件权威源，确认目录 `0700`、文件 `0600`；不得从聊天、工单或 shell 历史复制值。
3. 从 `.env.example` 创建根目录 `.env`；生产必须为 `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`，且不得设置非空 `COMPOSE_PROFILES` 或启用 `dev` profile，并填写 LDAP CA 文件与厂商 URL。LDAP 服务、DN、过滤器、属性和超时均在系统配置页维护，不得回填为环境变量。可信代理地址由 Compose 固定网络契约管理，不能通过环境变量覆盖；若调整网段，必须在同一提交同步修改 Web 静态 IP、API 的 `--forwarded-allow-ips` 和部署契约测试。API 直连来源的 X-Forwarded-For 不会被信任。`.env` 不得包含任何密码、Key 或 token。
4. 按 `vendor-egress.md` 完成主出口 IP、备出口 IP 同单报备，取得 QPS、单次号码上限和生效时间的书面回执。
5. 按 `backup-restore.md` 对当前生产库做一次加密备份并在隔离库恢复验证；未验证的备份不得作为回退点。
6. 执行 `sudo /usr/local/sbin/sms-compose config --quiet`，再核对 `sms_owner` secret 不会挂载到 api/worker/outbox-dispatcher/beat。
   PostgreSQL 的 `log_error_verbosity=terse`、`log_min_error_statement=panic`、`log_parameter_max_length_on_error=0` 是 PII 日志边界，禁止覆盖；错误类型仍保留，但失败 SQL、绑定参数与 DETAIL 不写容器日志。
7. 通过 systemd 或受控包装器启动栈，确认 postgres、migrate、api、三个 worker、Outbox dispatcher、beat、web 的健康与运行密钥 owner；生产不得启用 `dev` profile。
   API 发布端口固定只绑定宿主 `127.0.0.1`，外部业务流量必须经 Web/Nginx 入口；不得把 API 端口改为全网监听。
8. 使用已初始化的本地管理员进入系统配置页，保存并测试 AD 草稿后启用，导入经审批的目录组角色映射并完成真实 LDAP 四角色登录。禁止生产运行 mock seed。
9. 生产阶段让独立监控网段按 `prometheus.example.yml` 使用只读 `metrics_scrape_token` 抓取 `api:8000/metrics`，验证 `up == 1`；配置 `up == 0` 告警。端点同时校验来源 CIDR 与独立 Bearer secret，不得经公网入口开放。开发测试阶段按当前范围不执行生产监控或故障切换验收。
10. 按 `docs/UAT.md` 执行 28 项验收，确认 log-sink 已替换为经审批告警渠道且不泄露手机号/密钥。
11. 按 PRD 第 10 章执行 T0 “拉取权一刀切”，先 notice、再 verify、后 market；每应用观察 3 天后再迁下一批。
12. 归档变更记录、厂商回执、备份 SHA-256、权限检查、UAT 报告和监控截图；任一关键检查失败立即停止，不带病切换。

## 重启、`/run` 丢失与回退演练

在隔离 Mock 栈依次执行并记录 systemd/container 健康、密钥文件名/mode/UID/GID 与监听端口；禁止读取值、长度、摘要或哈希：

1. `sudo systemctl restart sms-platform.service`；
2. `sudo systemctl restart docker.service`；
3. 先 `sudo systemctl stop sms-platform.service`，再次确认目标目录属于本项目且没有真实数据或真实厂商发送，再删除 `/run/sms-platform/secrets`，随后 `sudo systemctl start sms-platform.service`；
4. 执行 `sudo reboot`，重连后检查 `sudo systemctl status sms-platform.service`。

每次必须自动重建 `current` generation 并恢复健康，且公网仍只允许 `18080/18443`，`80/443/8080/8443/8000/9028` 继续禁用。失败时禁止手工 chmod `0644`、改 root 容器、绕过预处理器或部分启动新容器。

代码回退先停止 unit，恢复上一已审核版本的 Compose、包装器、预处理器和 unit，再执行 `systemctl daemon-reload` 与受控启动。`rotate backend` 在共享 lifecycle flock 内执行整个 current-target→prepare→重建→可能回切/恢复链；与 `up`、`down` 或 migrate 并发时也会在任何 Python/Docker 操作前失败。无论轮换成功或失败都保留旧 generation，因为未重建的 PostgreSQL 仍可能挂载它。只有全栈已停止或全部容器已重建，并用固定 `ps --all -q`/容器挂载证据确认无引用后，才允许受控清理旧 generation。代码回退不删除 18 件权威源、数据库卷或旧 generation。若彻底撤销 systemd 托管，执行 `sudo systemctl disable --now sms-platform.service`，但不得把后续生产操作切回 raw Compose。

### v1.6.11 回调事件迁移前置检查

升级 0012 前必须暂停 callback worker 并清空或完成所有 pending/retrying 回调。迁移会对 legacy `message.report` 引用执行严格预检：重复复合消息引用、消息分区已缺失或报告字段不完整都会 fail-closed，错误仅包含安全任务标识/计数。出现失败时先在隔离库按变更单核对并处置相关任务，禁止删除约束、填充空 key 或强行 stamp 版本。`callback_report_event` 保留窗口跟随 `sys_config.raw_log_retention_days`，housekeeping 只删除已无任何任务引用的旧事件。产生同一消息多事件后，0012 downgrade 会在破坏性 DDL 前 fail-closed；必须前滚恢复或按变更单处置，禁止强行 stamp 旧版本。

## Prometheus 验收清单

Prometheus 仅从 `METRICS_ALLOWED_CIDRS` 指定的监控网段访问 API 内网地址，并通过 `authorization.credentials_file` 提交独立、可轮换的 `metrics_scrape_token`；公网防火墙和 Nginx 明确拒绝 `/metrics`。抓取超时固定 3 秒。开发测试阶段不执行生产监控/故障切换验收；进入生产准备后再按本节留存证据。部署后确认以下十二组 family 均存在：

- `sms_queue_depth`
- `sms_send_rate_per_second`
- `sms_vendor_error_chunks`
- `sms_uncertain_chunks`
- `sms_callback_failures`
- `sms_frequency_filtered_messages`
- `sms_poll_lag_seconds`
- `sms_usage_projection_drift_dimensions`
- `sms_usage_projection_drift_absolute_delta`
- `sms_worker_stalled_leases`
- `sms_worker_lease_events`
- `sms_metrics_snapshot_age_seconds`

`up == 0`、抓取超时或任一关键 family 消失均视为监控故障，不得用缓存旧值伪装健康。

## 数据密钥轮换格式

首次部署的 `data_aes_key`、`data_hmac_key` 均为 32 字节随机值的 base64 文本，自动视为 v1。轮换时不新增 secret 名，而是把两个文件同步改为 JSON keyring：

```json
{"active_version":2,"keys":{"1":"<旧base64>","2":"<新base64>"}}
```

两个 keyring 的版本集合和 `active_version` 必须一致。先保留旧版本完成历史数据解密/查询验证，再按 DBA 变更单重加密；不得提前删除仍被记录引用的版本。替换文件后需滚动重启 API、workers 与 beat，禁止把 keyring 内容打印到日志或工单。

### Callback HTTPS、私有 CA 与可选 mTLS

生产 callback URL 强制 HTTPS；HTTP 只允许显式 development/test Mock。每次投递都会重新
解析 DNS、校验 `callback_allow_cidrs`、固定当次已批准 IP，并以原域名作为 Host 与 SNI；
不跟随重定向。连接池、5 秒总预算、单地址连接预算以及响应头/正文上限由代码固定，禁止
通过部署参数放宽。

标准生产 18 件 secret 契约不因默认 HTTPS 改变。私有 CA 或 mTLS 属于可选的独立部署
变更：只有在安全评审同步扩展凭据清单和 Compose 只读挂载、且私钥仅对
`worker-callback` 可见后，才可在 `.env` 配置
`CALLBACK_CA_CERTS_FILE`、`CALLBACK_MTLS_CERT_FILE` 与
`CALLBACK_MTLS_KEY_FILE` 的挂载路径。证书或私钥内容不得进入 `.env`、镜像、Git、日志或
工单；证书与私钥必须成对配置，任一文件不可读时进程启动失败。未完成该部署变更时保持
mTLS 配置为空，仅使用系统 CA 验证 HTTPS。

历史通用 AAD/SMSX1 的双读与逐批重加密步骤见
`docs/context-encryption-migration.md`。新写入一律使用上下文绑定 v2/SMSX2；禁止为迁移
生成明文中间文件或提前移除仍被历史记录引用的 key version。
