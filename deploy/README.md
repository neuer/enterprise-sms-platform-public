# 生产部署索引

API 容器固定运行两个 Uvicorn worker，以保留强制性能门禁所需的并发余量。两个 worker 都在 API lifespan 内启动任务心跳服务，但 PostgreSQL 会话级 advisory lock 只允许一个进程执行巡检；领导进程退出或数据库连接中断后锁自动释放，存活进程在下一轮接管。不得把该巡检迁移为 beat 任务，也不得通过降低性能阈值替代容量基线。

`deploy/docker-compose.yml` 是服务名和队列名的基础契约；生产必须由受控入口同时叠加 `docker-compose.production-storage.yml`，`isolated-standalone` 还必须叠加 `docker-compose.redis-tls.yml`。三份文件共同定义 volume 与 25 件运行 secrets，禁止操作者自行选择、删减或改序。生产变更必须先在同版本预生产或隔离环境执行；不要在生产主机直接试改 Compose。生产唯一入口为 `sudo /usr/local/sbin/sms-compose ...`：它始终显式读取项目根 `.env`；所有会改变运行态的生产动作先准备运行密钥并执行存储、volume、Redis TLS 与 Compose 失败关闭预检，只读诊断不会暗中创建或修复资源。

## 权威手册

- [secrets.md](secrets.md)：生产 25 件权威源 `0700/0600`、UID 专属 `0400` 运行副本、挂载矩阵与轮换检查。
- [database-roles.md](database-roles.md)：七个运行职责、显式授权与 fail-closed 回滚。
- [dba.md](dba.md)：`sms_owner` 边界、审计不可变与分区生命周期。
- [storage.md](storage.md)：单台 12 vCPU/48 GiB 生产 VM 的五块固定 VMDK、七个 bind-backed named volume、70/80/90 阈值与在线扩容。
- [production-resource-responsibility-freeze.md](../docs/runbooks/production-resource-responsibility-freeze.md)：Ubuntu 24.04.4、VMware/磁盘/网络/制品/PKI/监控资源冻结，RACI、宿主只读预检与安全初始化顺序。
- [backup-restore.md](backup-restore.md)：无明文落盘的加密备份、校验与隔离恢复。
- [vendor-egress.md](vendor-egress.md)：Phase 0 主出口报备、后续备出口启用条件、QPS 与错误码 1010 验证。
- [controlled-real-vendor-test.md](../docs/runbooks/controlled-real-vendor-test.md)：开发测试环境使用正式 Key 的受控真实联调、100 计费条上限与停发规则。
- [test-fast-update.md](../docs/runbooks/test-fast-update.md)：真实联调期间的安全快速更新范围、固定命令和失败关闭流程。
- [usage-ledger-recovery.md](../docs/runbooks/usage-ledger-recovery.md)：配额/频控事实解释、Redis 投影漂移复核与安全重建。
- [redis-ha.md](redis-ha.md)：broker/auth/control 故障域、ACL、托管高可用、切换恢复和密码轮换。
- [database-pool-recovery.md](../docs/runbooks/database-pool-recovery.md)：分进程连接预算、指标、故障恢复与 24 小时混合负载留证。
- [prometheus.example.yml](prometheus.example.yml)：内网 Prometheus scrape 样例。
- [failover.md](failover.md)：Phase 0 单主恢复、旧系统切回边界与 RTO≤12h/RPO≤24h 手册。
- `seed.example.sql`：真实 LDAP 模式 role_mapping 示例；生产禁止执行 `seed-dev`。

上线切换业务顺序以 **PRD.md 第 10 章**为准，需人工签收事项以
**HANDOVER.md 第 1 节**为准。两者与本目录文档冲突时以 PRD 为准并记录变更单。

## VPN 入口与端口边界

- Web 明文 HTTP 上游默认绑定宿主机回环 `127.0.0.1:18080`，映射到容器内非特权 Nginx `8080`；direct 模式生产强制回环。远程 TLS 终结器场景必须显式设置 `SMS_EXTERNAL_TLS_MODE=1`、将 `WEB_BIND_IP` 设为专用私网接口，并用精确 `SMS_TRUSTED_PROXY_CIDRS` 同时限制代理头信任与明文 listener 来源；其余来源由 Nginx 返回 444。
- `18443` 预留给配置证书后的 TLS 终结服务；Phase 0 只对经批准的公司 VPN
  来源开放，未配置证书前不得宣称 HTTPS 可用。
- API `8000` 与 dev profile 的 Mock `9028` 只绑定宿主机回环地址，不得直接暴露到公网。
- 明文 HTTP 上游默认绑定回环，仅允许本机或显式批准的 TLS 终结器访问；主机防火墙与
  Docker `DOCKER-USER` 转发链必须阻断 `80/443/8080/8443/8000/9028` 的互联网入站。
  `18443` 也不对互联网公开，只接受获批公司 VPN 网段；网络侧真实读回是上线证据。

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

Callback 同样实行双层边界：根 `.env` 的 `CALLBACK_EGRESS_ALLOWED_CIDRS` 与 `CALLBACK_EGRESS_ALLOWED_PORTS` 是部署者不可变最大集合，管理员 `callback_allow_cidrs` 只能配置其子网。生产 CIDR 为空时拒绝全部 callback，端口缺省仅 443；保存 URL 与实际投递都会复验网段和端口。扩大部署上限必须走受控部署并重启 API 与 callback worker，不能通过系统配置页完成。

## 安装受控包装器与 systemd

### Security-sensitive boundary

认证、API Key、加密、幂等、短信发送任务、会话面、迁移、部署和 CI workflow 属于安全敏感边界。目录默认由 `deploy/scripts/protected_path_policy.py` 定义，并与 `.github/CODEOWNERS` 一致。这些区域的变更必须经过独立 Code Review；仓库不提供 owner 自动合并 workflow，required reviews 与 ruleset 不得被旁路。

生产主机必须以 root 安装 host-only 配置、包装器链接与 unit。`/etc/sms-platform/compose.env` 只能包含示例中的六个路径/模式变量：项目根、secrets mode、运行时 secret 根、厂商凭据根、真实联调状态根和控制 socket 根；不得复制项目根 `.env`，也不得出现 25 件 secret 名或值。

下列安装必须在专用生产主机的 Docker 首次启动前完成，并以 [storage.md](storage.md) 的五块
VMDK、UUID fstab、七个 Compose bind 源目录加独立备份目录（共八个固定子目录）及
owner/mode 真实读回为前置。若 Docker 已经启动或其
数据位于 OS 盘，先停机并按存储手册受控处置；禁止让应用安装命令自动搬迁
`/var/lib/docker/volumes/*/_data`。

首次安装只使用固定资产安装器；`CANDIDATE_COMMIT` 必须从受限变更单复制为本机 `HEAD` 的
40 位小写 SHA。占位字符串会被入口拒绝，不能原样执行：

```bash
CANDIDATE_COMMIT='REPLACE_WITH_40_LOWERCASE_HEX_COMMIT'
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py plan \
  --expected-commit "$CANDIDATE_COMMIT"
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py apply \
  --expected-commit "$CANDIDATE_COMMIT" \
  --confirm-dedicated-production-host \
  --confirm-vcenter-storage-reviewed
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py status
```

该入口把上述 17 个普通文件及 `/usr/local/sbin/sms-compose` 固定链接作为闭集安装，拒绝覆盖
任何已有目标，其中存储检查固定安装为 `/usr/local/sbin/sms-storage-preflight`；并把来源 commit
与逐文件摘要写入最后提交的 root-owned 状态文件。它不执行
APT/Git/磁盘/fstab/mount、secret、`.env`、Docker/Compose 或 systemd 生命周期动作。`status`
只有在状态和全部资产完整一致时才退出 0；`absent/incomplete/drifted` 都非零。apply 中途失败时
不得手工删除后重跑，须保留 `status` 输出并建立宿主资产恢复变更。

闭集具体包括 `compose.env`、`sms-storage-preflight`、`sms-storage-preflight.service`、
`10-sms-platform-storage.conf`、`10-storage-preflight.conf`、`sms-platform.service`、
`vendor-control-agent.service`、`lifecycle.json`、`lifecycle.env`，以及 partition、backup、
restore-drill、lifecycle-status 各自的 service/timer，共 17 个普通文件和一个 wrapper symlink。

该安装器是首装入口，不是宿主资产升级器。常规 release 不会更新上述 17 个 root-owned 文件；
`sms-compose` 又是指向 production operator 可写 checkout 的 symlink，因此该 operator 必须按
root-equivalent 受信身份管理。受控宿主资产升级/漂移围栏闭合前，任何修改该 inventory 或
wrapper 的生产候选都为 No-Go。security-report collector 不在本 inventory 内，当前不得手工
补装到生产；其第五镜像、Runtime 路径和日志轮转阻断见安全日报手册。

安装成功后再做只读 unit 验证和显式宿主动作：

```bash
sudo systemd-analyze verify /etc/systemd/system/sms-platform.service
sudo systemd-analyze verify /etc/systemd/system/sms-storage-preflight.service
sudo systemd-analyze verify /etc/systemd/system/vendor-control-agent.service
sudo systemd-analyze verify \
  /etc/systemd/system/sms-partition-maintenance.service \
  /etc/systemd/system/sms-backup.service \
  /etc/systemd/system/sms-restore-drill.service \
  /etc/systemd/system/sms-lifecycle-status.service
sudo systemctl daemon-reload
sudo systemctl start sms-storage-preflight.service
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/host_python_preflight.py lifecycle
```

宿主固定 `/usr/bin/python3` 必须精确为 Python 3.12；上述 preflight 还会验证 lifecycle 所需标准库
模块，失败时不得把 unit 改指项目虚拟环境或其他解释器。安装前人工复核
`/etc/sms-platform/compose.env` 恰好六行有效配置，mode 为 `0600`；只检查变量名、路径与 mode，
不读取任何权威文件。备份口令必须持久落在仓库外固定边界：

```bash
sudo install -d -o root -g root -m 0700 /etc/sms-platform/backup-secrets
sudo install -o root -g root -m 0600 /secure/staging/sms-backup-passphrase \
  /etc/sms-platform/backup-secrets/sms-backup-passphrase
sudo install -o root -g root -m 0600 \
  /secure/staging/recovery-crypto-generation-id \
  /etc/sms-platform/recovery-crypto-generation-id
sudo install -o root -g root -m 0600 \
  /secure/staging/backup-passphrase-generation-id \
  /etc/sms-platform/backup-secrets/generation-id
sudo test -f /etc/sms-platform/recovery-crypto-generation-id
sudo test ! -L /etc/sms-platform/recovery-crypto-generation-id
sudo test -f /etc/sms-platform/backup-secrets/generation-id
sudo test ! -L /etc/sms-platform/backup-secrets/generation-id
test "$(sudo stat -c '%U:%G:%a' /etc/sms-platform/recovery-crypto-generation-id)" \
  = 'root:root:600'
test "$(sudo stat -c '%U:%G:%a' /etc/sms-platform/backup-secrets/generation-id)" \
  = 'root:root:600'
```

三个源文件必须由受控密钥/备份保管流程生成并另有离机 escrow，命令和输出不得显示值、长度或
摘要；禁止改回重启即消失的 `/run` 路径。两个 generation ID 只是 1–64 字节 ASCII 的非敏感
标签（字符集 `[A-Za-z0-9._-]`，首字符必须是字母或数字，可有一个结尾换行），**不是**密钥/
口令材料的摘要、MAC 或密码学绑定证明；代码只比较 manifest 与主机上的标签，无法证明材料
来自该 ID 对应的 escrow bundle。离机 escrow 必须以 ID 管理原子、不可变的 recovery-crypto
bundle（data AES/HMAC、四个审计 keyring，按需含 alert pair）和 backup passphrase，双人见证
整包发放与一次性恢复机 provision；只要 35 天保留期内仍有 snapshot 引用，就必须保留相应
材料。报告中的标签匹配与实际探针成功只能证明当次已安装材料能读取已覆盖的样本，不能证明
escrow provenance、未抽样历史行或未来仍可取得材料。

示例 lifecycle 配置只含上述固定 ID 路径、口令路径、保留期和 RPO/工程恢复目标，不含口令或
密钥值。三个 lifecycle service 的 `ReadOnlyPaths=/etc/sms-platform` 只读覆盖这两个 ID 与备份
配置，只有备份保管流程可以在 service 停止后更新它们；不得为方便轮换扩大 systemd 写权限。
到首个 `release bootstrap` 成功前，
`sms-platform.service` 和四个 timer 必须保持未启用、未启动；全新主机禁止用普通 `up`
代替 bootstrap。bootstrap 成功后的 systemd 接管命令见首次引导章节。包装器要求动作是第一个
参数，只允许受控 `up`、`down`、`rotate backend`、
精确 `run --rm migrate`、`partition-maintenance [--dry-run]`、一次性 `init-admin`，以及
不会创建、启动或改变容器生命周期的 `config`、`ps`、`logs` 只读诊断入口；生产通用 `exec`
不属于只读诊断能力，不得用于数据库恢复、SQL、Redis 或容器内变更。其他生命周期和未知动作
全部退出 2。包装器拒绝 `--profile`、第二个 `--env-file` 等 Compose 全局参数作为首参，内部固定 `--env-file <项目根>/.env`。production 的 `up`、`rotate`、`run --rm migrate`、`partition-maintenance`、`init-admin` 会只读校验根 `.env` 的 `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`，并拒绝 shell 或 `.env` 中任何非空 `COMPOSE_PROFILES`。缺失、重复、不安全设置或 Compose 展开失败都会在启动前阻断。

开发测试服务器要启用真实联调控制台时，先安装 `deploy/systemd/compose.vendor-test.env.example` 和同 commit 的固定 agent unit，再由 root 执行 `sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform SMS_SECRETS_MODE=development /usr/local/sbin/sms-compose vendor-test bootstrap`。bootstrap 会在 lifecycle lock 内验证纯 Mock 根配置、固定路径和 unit 后执行 `systemctl enable --now`；不得用手工命令绕过这些检查。随后用 `systemctl is-active` 及 `stat` 只核对 `/run/sms-platform/vendor-control/vendor-control.sock`、`control-state.json` 的 owner/mode/类型。Compose 只把独立 UDS 目录只读挂载给 API 与 realtime worker，不挂载 `/run/sms-platform/secrets` 或 Docker Socket。GetBalance 由持有同一 lifecycle lock 的控制流程以 `run --rm --no-deps` 创建一次性 realtime worker 执行；命令仅调用 GetBalance，结束即删除，不把厂商凭据授予 API。agent 安装和应用发布都不得自动进入 controlled；正常的凭据、号码、激活、暂停、恢复和单号码 UAT 操作只能在系统配置页完成。页面轮换由 agent 先恢复私有 `rotation-state.json` 或孤儿 pending generation，再在 lifecycle lock 内保留既有暂停、停止发送进程、确认活动计数清零、建立加密 checkpoint、持久化 previous/new/phase、切换 generation、重建后端并用 GetBalance 验证；失败时写入不受 manual pause 遮蔽的独立 critical 层，回切并重建旧 generation，旧运行态再次通过 GetBalance 后才清理事务。marker 缺失时替换首次激活前凭据还必须再次证明根 `.env` 仍是严格纯 Mock，否则 fail closed。数据 HMAC keyring 增加版本后，active 测试号码须在页面重录同一号码刷新索引，否则真实 UAT fail-closed。版本化凭据根目录禁止进入发布、备份或恢复包，具体边界见受控真实联调手册。

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
在 operator 自有的临时 Git 仓库中从活动 checkout 的同一 `origin` 取得目标 object，再由
该 object 生成 root-owned 固定 staging。活动 checkout 的 `.git` 按部署合同保持 group
只读；不得为此临时放宽权限，也不得改用 root 在活动 checkout 中 fetch。以下命令中的
commit 必须是已审核并已推送目标分支解析出的完整 40 位 SHA：

```bash
set -euo pipefail
TARGET_COMMIT=<40位目标SHA>
TARGET_BRANCH=<目标分支>
SOURCE_ROOT="/var/lib/sms-platform/test-secure-access-bootstrap/source-$TARGET_COMMIT"
SOURCE_GIT="$(mktemp -d /tmp/sms-test-host-source.XXXXXX)"
cleanup_source_git() {
  rm -rf -- "$SOURCE_GIT"
}
trap cleanup_source_git EXIT
ORIGIN_URL="$(git -C /opt/sms-platform remote get-url origin)"
git init --bare "$SOURCE_GIT"
git -C "$SOURCE_GIT" remote add origin "$ORIGIN_URL"
git -C "$SOURCE_GIT" fetch --no-tags --depth=1 origin \
  "refs/heads/$TARGET_BRANCH:refs/test-secure-access/bootstrap"
test "$(git -C "$SOURCE_GIT" rev-parse \
  refs/test-secure-access/bootstrap^{commit})" = "$TARGET_COMMIT"
git -C "$SOURCE_GIT" cat-file -e "$TARGET_COMMIT^{commit}"
sudo install -d -o root -g root -m 0700 \
  /var/lib/sms-platform/test-secure-access-bootstrap
if sudo test -e "$SOURCE_ROOT"; then
  sudo mv -- "$SOURCE_ROOT" \
    "$SOURCE_ROOT.previous.$(date -u +%Y%m%dT%H%M%SZ)"
fi
sudo install -d -o root -g root -m 0700 "$SOURCE_ROOT"
git -C "$SOURCE_GIT" archive "$TARGET_COMMIT" -- \
  deploy/scripts/install_test_secure_access.py \
  deploy/scripts/test_secure_access_contract.py \
  deploy/scripts/test_secure_access_runtime.py \
  deploy/scripts/test_secure_access_manager.py \
  deploy/scripts/cloudflare_tunnel_manager.py \
  deploy/scripts/vendor_test_files.py \
  deploy/scripts/check_test_update_migration.py \
  deploy/scripts/run_with_lifecycle_lock.py \
  deploy/scripts/public_cutover_bootstrap.py \
  deploy/scripts/public_baseline_activation.py \
  deploy/scripts/public_baseline_manager.py \
  deploy/scripts/test_update_apply.py \
  deploy/scripts/test_update_backup.py \
  deploy/scripts/test_update_contract.py \
  deploy/scripts/test_update_manager.py \
  deploy/scripts/test_update_promote.py \
  deploy/scripts/test_update_store.py \
  deploy/scripts/test_update_verify.py \
  deploy/scripts/protected_path_policy.py \
  scripts/check_public_readiness.py \
  scripts/export_public_snapshot.py \
  scripts/verify_public_snapshot_cutover.py \
  scripts/verify_web_transport.py \
  deploy/sms-compose \
  deploy/systemd/sms-platform-test-secure-access.service \
  deploy/systemd/sms-platform-cloudflare-tunnel.service |
  sudo /bin/tar -x -C "$SOURCE_ROOT"
sudo chmod 0644 \
  "$SOURCE_ROOT/deploy/scripts/install_test_secure_access.py" \
  "$SOURCE_ROOT/deploy/scripts/test_secure_access_contract.py" \
  "$SOURCE_ROOT/deploy/scripts/test_secure_access_runtime.py" \
  "$SOURCE_ROOT/deploy/scripts/test_secure_access_manager.py" \
  "$SOURCE_ROOT/deploy/scripts/cloudflare_tunnel_manager.py" \
  "$SOURCE_ROOT/deploy/scripts/vendor_test_files.py" \
  "$SOURCE_ROOT/deploy/scripts/check_test_update_migration.py" \
  "$SOURCE_ROOT/deploy/scripts/run_with_lifecycle_lock.py" \
  "$SOURCE_ROOT/deploy/scripts/public_cutover_bootstrap.py" \
  "$SOURCE_ROOT/deploy/scripts/public_baseline_activation.py" \
  "$SOURCE_ROOT/deploy/scripts/public_baseline_manager.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_apply.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_backup.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_contract.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_manager.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_promote.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_store.py" \
  "$SOURCE_ROOT/deploy/scripts/test_update_verify.py" \
  "$SOURCE_ROOT/deploy/scripts/protected_path_policy.py" \
  "$SOURCE_ROOT/scripts/check_public_readiness.py" \
  "$SOURCE_ROOT/scripts/export_public_snapshot.py" \
  "$SOURCE_ROOT/scripts/verify_public_snapshot_cutover.py" \
  "$SOURCE_ROOT/scripts/verify_web_transport.py" \
  "$SOURCE_ROOT/deploy/systemd/sms-platform-test-secure-access.service" \
  "$SOURCE_ROOT/deploy/systemd/sms-platform-cloudflare-tunnel.service"
sudo chmod 0755 "$SOURCE_ROOT/deploy/sms-compose"
sudo install -o root -g root -m 0644 \
  /tmp/cloudflared-linux-amd64 \
  /var/lib/sms-platform/test-secure-access-bootstrap/cloudflared-linux-amd64
sudo /usr/bin/env \
  "GIT_ALTERNATE_OBJECT_DIRECTORIES=$SOURCE_GIT/objects" \
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
rm -- /tmp/cloudflared-linux-amd64
```

每次使用新的 commit 专属 source 目录，旧目录只改名保留，不在其上覆盖解包；临时 Git
仓库只用于本次服务端 fetch/archive，并通过
`GIT_ALTERNATE_OBJECT_DIRECTORIES` 让安装器把 staged 字节绑定到同一目标 object；成功或
失败退出都会清理它，不向活动 `.git` 写对象或 ref。Git archive 只枚举 installer 实际
允许的固定资产，未枚举文件不会进入启动 `PYTHONPATH`。由于
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
并单独评审 PostgreSQL/volume 保留、25 件 secrets、三域 Redis、七职责数据库角色、回退与
证据清理。迁移完成后的服务器 HEAD 和 origin 必须直接绑定公开仓库 commit，且不得把
任何私有 URL、ref、commit 或对象带回公开工作区；随后才恢复
按需的 `scripts/test_update.sh apply --ref origin/main` 流程；`plan` 与 `status` 分别
只用于可选预览和后续诊断。唯一获准的一次性执行步骤见
`docs/runbooks/public-baseline-activation.md`。

该一次性流程的固定合同是：本地 driver 只执行
`prepare → build → finalize`，由 build 从 verified bundle 的原始 blob 规范化构建并冻结
API/Web 镜像，不接受手工 build plan 或 image ID；上传时
`public-baseline.bundle`、`api.tar`、`web.tar`、`baseline-manifest.json` 就绪后，最后才
发布 `request.json`。服务器 operator UID/GID 固定为 `1000:1000`，不动态推断；只使用
host bootstrap 的
`baseline-prepare/apply/verify/status/finalize/cleanup`。finalize 保留 recovery；
只有标准状态 verified 且表面验收完成，cleanup 才删除旧 root 与 bundle/API/Web 三个
大产物，并继续保留 manifest/request、test-update store 和 core journal。

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

### 持久 Cloudflare Named Tunnel

云服务商不能开放 80/443 时，长期入口使用 Cloudflare 远程管理的 Named Tunnel；Quick
Tunnel 仍只用于 15 分钟临时入口，不能替代本节。Cloudflare 侧先创建 Named Tunnel 和一条
公开主机名路由（例如 `sms.example.com -> http://127.0.0.1:18080`）。源站继续只监听回环，
`.env` 必须保持 `WEB_BIND_IP=127.0.0.1`、`SMS_EXTERNAL_TLS_MODE=1` 与
`SMS_TRUSTED_PROXY_CIDRS=127.0.0.1/32,172.31.250.1/32`：前者仅允许容器自检，后者是
固定 `sms-platform_ingress` 网关，承载 cloudflared 从宿主回环端口进入 Web 容器的请求。
不得开放宿主机 80/443，也不得把任意公网、整个 Docker 子网或整段 Cloudflare 地址加入
Nginx trusted proxy。`sms-compose up` 与测试更新都会从根 `.env` 渲染该合同；开发测试环境
缺少显式 `SMS_EXTERNAL_TLS_MODE` 时失败关闭。`cloudflare-tunnel start/verify` 还会同时核对
根 `.env` 和工作树外的渲染结果，防止 Tunnel 正常但客户端 IP 退化为 Docker 网关。

主机资产随上节的固定安装器完成 commit/digest 绑定，但持久 unit 不会自动安装、启动或
enable。操作者在 Cloudflare 控制台复制 Tunnel token 后，只能在服务器控制 TTY 中运行
`install-token` 并盲输；token 禁止进入命令参数、环境变量、shell history、Git、数据库、
日志或工单：

```bash
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  /usr/local/sbin/sms-compose cloudflare-tunnel install
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  /usr/local/sbin/sms-compose cloudflare-tunnel configure \
    --hostname sms.example.com
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  /usr/local/sbin/sms-compose cloudflare-tunnel install-token
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  /usr/local/sbin/sms-compose cloudflare-tunnel start
```

systemd 通过 `LoadCredential` 把 root `0600` token-file 暂时挂给 DynamicUser；进程列表中只
出现 credential 文件路径，不出现 token。`start` 先验证主机 manifest、unit、token 文件、
平台 service 与 `127.0.0.1:18080/login`，再执行 `enable --now`；任一步失败都关闭 Tunnel。
Cloudflare zone、权威 NS、Universal SSL、HTTP→HTTPS 与公开主机名就绪后执行无凭据探针：

```bash
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  /usr/local/sbin/sms-compose cloudflare-tunnel verify
```

只有 `status=verified`、TLS 1.2/1.3、证书剩余至少 14 天、HTTP 精确跳转 HTTPS、HSTS
`max-age>=31536000; includeSubDomains` 和 CSP/浏览器安全头全部通过，才可宣告长期 HTTPS
入口完成。回退时先恢复原权威 NS，再执行 `cloudflare-tunnel stop`；`stop` 会
`disable --now` 且确认 unit inactive，但保留 root-only 配置和 token 供受控恢复。DNSSEC
只能在 Cloudflare 激活稳定后单独启用并向注册商写入 Cloudflare 提供的 DS，切换 NS 前后
都不得遗留旧 DS。

### 与快速更新和数据的边界

需要共享测试环境验收的应用代码在合并后通过默认入口：

```bash
scripts/test_update.sh apply --ref origin/main
```

`apply` 内部已经完成分类、verify 与终态解析；`plan --ref origin/main` 和 `status` 分别只
用于可选预览与后续只读诊断。只有远端 `state=verified` 和对应表面验收完成才算更新成功。driver 只构建受影响镜像且
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

同历史旧测试基线若仍是目标 `origin/main` 的祖先、包含真实迁移前移，且普通 `apply` 仅被
合同枚举的旧运行凭据准备/撤销脚本与固定非运行态文件阻断，可在重装同一目标 commit 的
host-control 快照、确认完整五项 CI check 成功后执行：

```bash
scripts/test_update.sh rebaseline --ref origin/main
```

这是一次性基线修复，不是日常更新别名：它强制 API/Web、高风险暂停、不安全分片检查、
密文 checkpoint、expand-only 迁移、verify 与 operator Git 复核；不接受分支、无迁移、
跨历史或任意未枚举路径。当前批准的 `0053_idempotency_scope →
0061_vendor_binding_outbox` 路径还会在 checkpoint 后由服务器本机把精确旧 18 件权威密钥
清单可恢复地扩展为 24 件，再用目标预处理器生成 runtime generation；只追加四个独立审计
key 与一对匹配 X25519 key，不替换既有密钥或厂商凭据，也不输出值、长度、摘要或派生信息。
这是历史固定迁移，不能代生成 Phase 0 新增的第 25 件 `redis_tls_server_key`；该私钥必须走
独立运行密钥变更，并与内部 PKI 证书在预生产验真后才能更新目标版本。
若已切换目标 checkout 但 migration head 仍停在 0053 且状态精确为 `blocked/migrate`，只可
按 `docs/runbooks/test-fast-update.md` 的固定 `recover-rebaseline` 入口恢复旧 checkout/镜像
指针并保持 pause；该入口可在已回到请求 base 后幂等重放，并在最终 root Git 校验后恢复
operator 读路径，然后重新执行完整 rebaseline。日常更新继续只使用 `apply`，不得用
`rebaseline` 或该恢复入口绕过分类失败。
若迁移和应用切换已完成，唯一失败点为 `blocked/get_balance`，只可按同一手册使用
`recover-rebaseline-verify`：它要求从已审核提交新安装且自校验通过的不可变 host-control
快照；该快照提供恢复入口，身份独立于被恢复的应用提交。原请求 target 与当前 HEAD 必须
完全一致，migration head 必须为 `0061_vendor_binding_outbox`，且原 update 仍持有两条
pause；随后完整重跑 verify，不会重跑迁移或绕过真实厂商探测。调用必须使用本地
`scripts/test_update.sh recover-rebaseline-verify`，由它校验并传递受控厂商 origin；同一入口
在 GetBalance 成功且两条原 update pause 仍被持有时，仅启动被 fail-closed 停止的固定 backend
服务集合，再检查服务与 migration head；同时由 public-baseline manager 在活动根、manifest、
镜像和 operator Git 身份全部匹配后，把已安装 unit 限定为旧/目标两种已知字节并原子恢复目标
`vendor-control-agent.service`，重启后验真。不会手工改权限、强制重建或切换镜像。该入口产生且事件链可证明的
固定 verify step 再阻断允许重放，其他 blocked step 或身份漂移继续失败关闭。

页面 `reset_configuration` 的 root 撤销阶段只调用 development-only、零参数固定操作 `vendor-test reset-runtime`。该操作和 release 共用同一个 lifecycle flock：先切换到固定 revocation tombstone generation，再停止、删除和重建 worker-realtime、worker-bulk 两个厂商 secret reader，容器内无输出探测通过后才清理 stale runtime generations；API 不挂载厂商凭据，无需参与 reader 撤销。它不回切旧 generation，不删除 runtime root、PostgreSQL、Docker volume 或非厂商 secret，也不扩大 systemd capability、可写路径、网络族、sudo 或任意命令能力。任何部分失败由同一 journal operation 重放，固定错误不得携带 Key 或 generation 元数据。

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

目录组到平台角色的映射也在该区域维护。AD 账号首次成功登录后进入用户台账；登录名若已被本地身份先占用，将拒绝该次 AD 登录并记录来源冲突审计。生产部署仍必须提供 25 件 secrets，其中 LDAP bind 密码固定为 `ldap_bind_password`，CA 为只读受控文件而非 secret；未来 IAM Provider 仅预留扩展能力，本期不配置、不启用。

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

数据镜像变化时再追加 `--data-images`；PostgreSQL 镜像变化或 migration from/target 不同
时还必须追加 `--backup-record` 与 `--restore-report`。生成器会原子写入 `0600` 文件并调用现有
exact-field 契约自校验。最终目录清单必须与 manifest 声明完全相等。

`gate_type=release_control_smoke` 只由 `scripts/verify_release_control.sh` 在隔离 development project 中生成，用于配置失败、健康失败、自动补偿、TERM 中断和续跑的控制面证据。它明确 `scan_performed=false`，不替代 Trivy，不代表发布就绪，生产 mode 和 `scripts/deploy_release_remote.py` 均拒绝该证据。正式发布只能使用 `release_gate_kind=release`。

### development archive 与 production preloaded digest

- `development archive`：每个 changed 镜像必须提供清单点名的 tar 和 SHA-256；prepare 校验后执行受控 `docker load`。未变化镜像的 archive 字段必须为 null。此模式用于隔离 Mock/控制面演练，不得作为生产镜像来源。
- `production preloaded digest`：四个 ref 必须是受控仓库 `image@sha256:RepoDigest`，镜像须在目标主机预先拉取；清单 archive 字段全部为 null，prepare 不拉取网络资源，并逐一核对本地 image ID、`linux/amd64` 与 RepoDigests。PostgreSQL changed 或 migration from/target 不同时都必须附加同 commit 的加密备份变更记录和隔离恢复报告。

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

全新空生产主机不先执行普通 `up`。使用四镜像均为目标 digest、全部 `changed=false`、
migration from=target=head 的基线 manifest，并在人工确认固定 Compose 项目、七个 bind 源目录、
独立备份目录和 release 根均为空后执行一次：

```bash
python3 scripts/create_release_manifest.py \
  --release-report /secure/staging/production-baseline/release-gate.json \
  --output /secure/staging/production-baseline/manifest.json \
  --release-id production-baseline \
  --migration-from "$MIGRATION_HEAD" \
  --migration-target "$MIGRATION_HEAD" \
  --baseline
sudo /usr/local/sbin/sms-compose release bootstrap \
  --manifest /secure/staging/production-baseline/manifest.json \
  --confirm-empty-host
```

该命令在 lifecycle lock 内再次验证空主机、正式 release evidence、生产 topology、存储、TLS、
volume、镜像与精确 commit，按数据服务→migration→应用服务顺序启动，并把首个基线封存为
`succeeded` release。确认参数只表达当次人工确认，不会清理或初始化已有数据；发现任何容器、
volume、bind 源文件或 release 状态立即失败，失败后必须人工审计，禁止删除状态后重跑。

`release prepare` 是受控准备动作：验证证据、当前 commit/容器、镜像和迁移基线并写入 `/var/lib/sms-platform/releases/<release_id>`，但不改根 `.env` 或容器，也不取得 lifecycle lock；因此必须在没有 systemd/包装器生命周期操作的准备窗口执行。它把 Redis 模式、按序 Compose 文件及 hash、非镜像 `.env` 配置绑定到发布状态，activate/resume/rollback 发现漂移会在 Docker mutation 前失败。`release status` 只读且不取锁。`release activate/resume/rollback` 会准备运行密钥，并与 up/down/rotate/migrate 共用同一个 lifecycle flock；锁冲突时在任何 Docker 或状态修改前退出。成功状态重复调用幂等；succeeded 发布不能原地 rollback，也不能直接切回旧镜像或 downgrade schema。应在新 commit 中做保持当前 schema 的兼容修复或反向应用变更，生成并扫描新的 digest/manifest，再执行：

```bash
sudo /usr/local/sbin/sms-compose release prepare-forward-rollback \
  --source-release-id release-20260715 \
  --manifest /secure/staging/release-forward-fix/manifest.json
sudo /usr/local/sbin/sms-compose release activate --release-id release-forward-fix
```

前向回退候选必须保持 PostgreSQL/Redis 数据镜像与当前 schema，不得把旧 digest 包装成新发布。

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

`首次引导`时，从公司内部只读 Git 镜像把已审核的精确 commit、`deploy/sms-compose`、release
scripts 和 systemd unit 安装到空的 `/opt/sms-platform`，执行 `systemd-analyze verify`；此时不得
已有任何本项目容器或数据卷。四个 production digest 只能从内部 Registry 预加载，不在生产
主机源码构建。生产仓库的 `origin` 必须指向该内部只读镜像，禁止把 GitHub 作为生产主机的
直接依赖。发布工具不能自更新或替换自身；首个基线只能使用人工安装的同一精确 commit 执行
`release bootstrap`。后续才使用统一状态机和远端 driver。

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

driver 只用于首个 bootstrap 成功后的更新，不能承担空主机自举。它只以固定 argv 执行内部
只读 `origin` 的 Git fast-forward、逐文件哈希上传、prepare/activate/status 和 `/readyz` 探针，
不拼接远端 shell，也不从 GitHub 拉取或在生产主机源码构建。上传前会依次把暂存根和单次发布
目录收紧为 `0700`，避免首次递归建目录遗留可遍历的中间目录。每个文件始终写入同名固定
`.part`，`rsync --partial --inplace` 失败时最多自动重试 3 次；因此 macOS `openrsync` 或 SSH
长连接中断后可从同一个远端文件续传。只有远端 SHA-256 与本地清单一致才原子改名，三次仍
失败则在 Git、prepare 和 activate 前失败关闭并保留 `.part` 供下一次续传。它会把清单枚举后的
`SMS_SECRETS_MODE=development|production` 通过固定 `/usr/bin/env` argv 显式交给 sudo wrapper，
不依赖 sudo 环境继承；服务器 fetch 后还会要求内部获批 remote ref 精确解析为清单 commit，
已存在的 rollback ref 只读复核、绝不 force 覆盖。终态必须同时通过
`systemctl is-active --quiet sms-platform.service` 和
`git status --porcelain --untracked-files=normal` 空输出。

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
2. 按 `secrets.md` 从受控密钥系统落盘恰好 25 件权威源，确认目录 `0700`、文件 `0600`；主体 `audit_context_key` 与 API/realtime/bulk 三个自治审计 key 必须是四个两两独立的 32 字节 base64 key，企微公私钥必须是匹配的 X25519 对，`redis_tls_server_key` 必须与内部 PKI 的三 SAN 服务端证书配对。不得从聊天、工单或 shell 历史复制值。
3. 从 `.env.example` 创建根目录 `.env` 并设置 `root:root 0600`；生产必须为 `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`VENDOR_MOCK=0`、`REDIS_HA_MODE=isolated-standalone` 和固定 `METRICS_ALLOWED_CIDRS=172.31.250.1/32`，且不得设置非空 `COMPOSE_PROFILES` 或启用 `dev` profile。Phase 0 先安装可读的系统 CA 文件但保持 `LDAP_ALLOWED_HOSTS` 为空、AD Provider 禁用；AD 地址、CA、bind secret、连接测试和角色映射齐备后再走独立变更。JWT 密钥可使用版本化 keyring；新签发令牌固定带 `kid/iss/aud`，`JWT_ACCEPT_LEGACY` 只用于非生产的旧无 `kid` 令牌观察窗口，**生产必须为 false，禁止观察窗口**。LDAP 服务、DN、过滤器、属性和超时均在系统配置页维护，不得回填为环境变量；任何不在 `LDAP_ALLOWED_HOSTS` 中的目标在读取或使用 Bind Secret 前失败关闭。可信代理地址由 Compose 固定网络契约管理。内部 Nginx 默认丢弃客户端代理头并覆盖为 `$remote_addr`（`X-Real-IP`/`X-Forwarded-For`），`X-Forwarded-Proto` 仅在 `$sms_trusted_proxy=1`（连接来自精确受信 TLS 终结器）时接受 `http|https`。direct 模式强制 `WEB_BIND_IP` 为回环。外部 TLS 终结拓扑必须设置 `SMS_EXTERNAL_TLS_MODE=1`，并在 `SMS_TRUSTED_PROXY_CIDRS` 中逐个枚举 TLS 终结器（IPv4 `/32`、IPv6 `/128`；禁止 `0.0.0.0/0`、整个云 VPC、客户端可达网段或任何多主机网段）；这些精确地址同时控制 Nginx 明文 listener allowlist，非受信来源在进入页面或 API 前返回 444。`sms-compose up` 启动前由 `deploy/scripts/render_trusted_proxy_conf.py` 渲染 `/usr/local/share/sms-platform/trusted-proxies.conf`（Git 工作树之外，目录 0755、文件 0644），配置缺失、非法或非主机前缀时失败关闭。直连模式（`SMS_EXTERNAL_TLS_MODE=0`）保持代理头信任标志为 0，任何客户端代理头都不会被接受。若调整代理地址，必须在同一提交同步修改 Web 静态 IP（`SMS_WEB_INGRESS_IPV4`）、API 的 `--forwarded-allow-ips`（引用同一变量）和部署契约测试。API 直连来源的 X-Forwarded-For 不会被信任。`.env` 不得包含任何密码、Key 或 token。
4. 按 `vendor-egress.md` 完成固定主出口 IP 报备，取得 QPS、单次号码上限和生效时间的书面回执。备出口待后续建设并在启用前另行报备、验证，不阻断 Phase 0 首发。
5. 在预生产使用候选 digest 和预生产自有数据完成加密备份/隔离恢复；此时生产库仍必须为空，
   不得在 bootstrap 前声称存在“当前生产备份”。
6. 执行 `sudo /usr/local/sbin/sms-compose config --quiet`，再核对 `sms_owner` secret 不会挂载到 api/worker/outbox-dispatcher/beat。
   PostgreSQL 的 `log_error_verbosity=terse`、`log_min_error_statement=panic`、`log_parameter_max_length_on_error=0` 是 PII 日志边界，禁止覆盖；错误类型仍保留，但失败 SQL、绑定参数与 DETAIL 不写容器日志。
   API 必须使用 uvicorn `--no-access-log`；Nginx 只使用 `pii_safe` 格式（`$uri`，不得记录 query string，禁止改回 `$request`/`$request_uri`）。这是手机号不得进入访问日志的硬约束。
7. 全新主机先用上述 `release bootstrap --confirm-empty-host` 建立首个 succeeded 基线。成功并完成状态读回后，执行 `sudo systemctl enable --now sms-platform.service`；该次受控、幂等的 `up` 让 `Type=oneshot`/`RemainAfterExit` unit 正式进入 active，确保后续关机和重启受 systemd 管理，不能只 `enable` 而不启动。再执行 `sudo systemctl enable --now sms-partition-maintenance.timer sms-backup.timer sms-lifecycle-status.timer`，最后检查平台和这三个 timer；**生产不得启用 `sms-restore-drill.timer`**。migrate 会把主体 `audit_context_key` 与 API/realtime/bulk 三个自治审计 key（共四个）同步进仅 owner 可读的验证表。确认 postgres、migrate、api、三个 worker、Outbox dispatcher、beat、web 的健康与运行密钥 owner；生产不得启用 `dev` profile。升级到 v1.6.50 会清空旧企微 webhook 密文，必须在新 X25519 密钥对生效后由管理员重新配置并测试，禁止尝试回读或迁移旧明文。
   API 发布端口固定只绑定宿主 `127.0.0.1`，外部业务流量必须经 Web/Nginx 入口；不得把 API 端口改为全网监听。
8. bootstrap 后立即手动启动 `sms-backup.service`，确认 `backup-status` 通过；把首份生产密文
   快照经批准的离线通道送到具备独立 PostgreSQL/VMDK 和 preproduction marker 的恢复主机，
   按 `failover.md` 先停止该主机的平台与 lifecycle timers，再安装完整 25 件 canonical
   secret：其中 data AES/HMAC 与四个审计 key（确需读历史告警时再含 alert pair）
   只能使用离机 escrow 按 manifest 所记 generation ID 原子发放且双人见证的不可变 recovery
   bundle，其余使用当前获批运行凭据；同时安装
   backup passphrase、两个固定 generation ID 和内容精确为
   `preproduction-restore-host-v1\n` 的 marker。先通过包装器公开参数执行
   `release start-recovery` 来生成/绑定 runtime generation 并只启动四个数据服务，
   再以显式 snapshot/manifest 调用官方 `restore_drill.py` 并配对报告；不得使用普通
   `up`、通用 `exec`、手工 prepare 或修改 `/run`，也不得
   复制/手改生产 lifecycle state，也不得让预生产账本猜选候选。Phase 0 只允许使用从预生产
   资源池按需创建的一次性空白隔离恢复机；共享应用预生产不得安装生产密码学材料或承担该演练。
   证据归档后按批准的 VMware 流程整体退役恢复机。该证据通过前不得初始化管理员、创建
   API Key 或开放 T0 流量。
   此时生产 `sms_message` 应为 0；schema v2 恢复报告的
   `crypto_probe_receipts.pre_migration`/`post_migration` 必须各自显示 17 个固定密文字段、
   `counts.encrypted_rows=0`、全部 `coverage.rows=0` 与 `status=not_applicable_empty`，绑定
   pre 的汇总检查也只能为 `not_applicable_empty`。这只作为**初始化前**空库结果，不能证明
   data AES/HMAC 能读取未来生产历史密文。首批最小 notice 形成事实后，必须在 3 天观察期内
   再生成一份生产快照，并在新的一次性空白隔离恢复机复验；报告须有
   `table_counts.sms_message>0`，两份 receipt 按 17 字段精确闭集逐字段逐实际版本抽样认证，
   且 pre/post 与绑定检查均为 `performed`。抽样结果不得外推为所有历史行、alert 历史密文/
   keypair 或 `audit_log` MAC 已验证；未取得该证据不得扩大到更多应用、verify 或 market。
   通过后才使用正式入口初始化本地管理员、完成首次改密，并保持 local Provider 为 Phase 0
   唯一启用认证源。AD 草稿、连接测试、角色映射和启用留待地址、CA、bind secret 与组织映射
   齐备后的独立变更。禁止生产运行 mock seed。
9. 在生产 VM 宿主安装本机 Prometheus 或等价采集 agent，按 `prometheus.example.yml` 从 Docker ingress 网桥访问 `172.31.250.2:8000/metrics`。采集连接在 API 容器内应读为网桥网关 `172.31.250.1`；真实抓包/日志读回一致后，生产 `METRICS_ALLOWED_CIDRS` 必须收紧为 `172.31.250.1/32`，不得放行整个 `/24`。把独立 token 的受控副本落到 `/etc/prometheus/secrets/metrics_scrape_token`，`root:prometheus 0440`，验证 `up == 1` 并配置 `up == 0` 告警。该端点不经 Nginx 或公网开放。由于采集器与业务同 VM，VM 宕机告警必须另由 VMware/宿主外监控产生；两类告警都由外部日志/告警转发链送达企业微信和公司邮件。开发测试阶段按当前范围不执行生产监控或故障切换验收。
10. 在预生产按 `docs/UAT.md` 完成 28 项适用验收；生产首发只对 local Provider、1–2 个低风险 notice 应用及对应安全/运维边界做真人复验，确认企业微信和公司邮件均有真实接收证据且不泄露手机号/密钥。AD 用例延后到 Provider 启用前。
11. 按 PRD 第 10 章只接入首批 1–2 个 notice 应用，每个连续观察至少 3 天。verify、market 或更多应用另行审批，旧系统继续长期并行。
12. 归档变更记录、厂商回执、备份 SHA-256、权限检查、UAT 报告和监控截图；任一关键检查失败立即停止，不带病切换。

## 重启、`/run` 丢失与回退演练

在隔离 Mock 栈依次执行并记录 systemd/container 健康、密钥文件名/mode/UID/GID 与监听端口；禁止读取值、长度、摘要或哈希：

1. `sudo systemctl restart sms-platform.service`；
2. `sudo systemctl restart docker.service`；
3. 先 `sudo systemctl stop sms-platform.service`，再次确认目标目录属于本项目且没有真实数据或真实厂商发送，再删除 `/run/sms-platform/secrets`，随后 `sudo systemctl start sms-platform.service`；
4. 执行 `sudo reboot`，重连后检查 `sudo systemctl status sms-platform.service`。

每次必须自动重建 `current` generation 并恢复健康，且 `18443` 仍只允许经审批的公司 VPN
网段，不对互联网公开；`80/443/8080/8443/8000/9028` 与明文上游端口继续禁用。
失败时禁止手工 chmod `0644`、改 root 容器、绕过预处理器或部分启动新容器。

上面的重启和 `/run` 删除步骤**只限隔离 Mock 演练**，不得在生产中通过恢复旧 Compose、包装器、
预处理器或 unit 来做代码回退。生产 succeeded release 的常规回退只能构建新 commit、新 digest、
保持当前 schema 的 forward-rollback manifest，再执行 `prepare-forward-rollback` 和 `activate`；
发布工具自身也必须随新候选评审，禁止停 unit 后手工替换旧工具绕过 release state。
`recovery_required` 仅允许在保持维护窗口时，由 DBA、发布负责人和安全复核人根据归档 state/event、
容器和 migration 只读证据批准专项恢复方案，不得伪装成常规回退。

`rotate backend` 在共享 lifecycle flock 内执行整个 current-target→prepare→三 Redis 与全部客户端
重建→可能回切/恢复链；与 `up`、`down` 或 migrate 并发时也会在任何 Python/Docker 操作前
失败。无论轮换成功或失败都保留旧 generation，因为未重建的 PostgreSQL 仍可能挂载它。只有
全栈已停止或全部容器已重建，并用固定 `ps --all -q`/容器挂载证据确认无引用后，才允许受控
清理旧 generation。任何回退都不删除 25 件权威源、数据库卷或旧 generation。若经独立退役
变更彻底撤销 systemd 托管，可执行 `sudo systemctl disable --now sms-platform.service`，但不得把
后续生产操作切回 raw Compose。

### v1.6.11 回调事件迁移前置检查

升级 0012 前必须暂停 callback worker 并清空或完成所有 pending/retrying 回调。迁移会对 legacy `message.report` 引用执行严格预检：重复复合消息引用、消息分区已缺失或报告字段不完整都会 fail-closed，错误仅包含安全任务标识/计数。出现失败时先在隔离库按变更单核对并处置相关任务，禁止删除约束、填充空 key 或强行 stamp 版本。`callback_report_event` 保留窗口跟随 `sys_config.raw_log_retention_days`，housekeeping 只删除已无任何任务引用的旧事件。产生同一消息多事件后，0012 downgrade 会在破坏性 DDL 前 fail-closed；必须前滚恢复或按变更单处置，禁止强行 stamp 旧版本。

## Prometheus 验收清单

Phase 0 的 Prometheus/agent 位于生产 VM 宿主，只从固定 ingress 地址
`172.31.250.2:8000` 访问 API；API 观察到的宿主网桥源地址必须读回并以精确 `/32` 写入
`METRICS_ALLOWED_CIDRS`。它通过 `authorization.credentials_file` 从
`/etc/prometheus/secrets/metrics_scrape_token` 提交独立、可轮换的
`metrics_scrape_token`；公网防火墙和 Nginx 明确拒绝 `/metrics`。抓取超时固定 3 秒。
宿主内采集不能证明整 VM 存活，必须另有 VMware/宿主外心跳告警。生命周期、存储预检和
Docker unit 的固定 `journal` 事件只提供信号，外部采集/告警链负责送达企业微信与公司邮件；
两个渠道的真实接收回执才构成闭环。开发测试阶段不执行生产监控/故障切换验收；进入生产
准备后再按本节留存证据。部署后确认以下十二组 family 均存在：

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
不跟随重定向。固定 IP 请求禁用 keep-alive，避免两个逻辑主机共享 IP/端口时复用首个
主机已认证的 TLS 连接；连接并发、5 秒总预算、单地址连接预算以及响应头/正文上限由代码
固定，禁止通过部署参数放宽。

标准生产 25 件 secret 契约不因 callback 默认 HTTPS 改变。私有 CA 或 mTLS 属于可选的独立部署
变更：只有在安全评审同步扩展凭据清单和 Compose 只读挂载、且私钥仅对
`worker-callback` 可见后，才可在 `.env` 配置
`CALLBACK_CA_CERTS_FILE`、`CALLBACK_MTLS_CERT_FILE` 与
`CALLBACK_MTLS_KEY_FILE` 的挂载路径。证书或私钥内容不得进入 `.env`、镜像、Git、日志或
工单；证书与私钥必须成对配置，任一文件不可读时进程启动失败。未完成该部署变更时保持
mTLS 配置为空，仅使用系统 CA 验证 HTTPS。

历史通用 AAD/SMSX1 的双读与逐批重加密步骤见
`docs/context-encryption-migration.md`。新写入一律使用上下文绑定 v2/SMSX2；禁止为迁移
生成明文中间文件或提前移除仍被历史记录引用的 key version。
