# 正式 Key 受控真实厂商联调手册

## 适用范围与红线

本手册只适用于仍处在开发测试阶段、没有其他系统共用厂商账号的单机测试服务器。目标是停止业务 Mock 发送，使用完整功能的正式 Key 做小流量真实联调；仓库单元测试、CI、G2 和故障注入继续使用 Mock/FakeCarrier。

**正常操作均在系统配置页完成**。管理员只从 `/configs` 的「真实联调」页安装或轮换凭据、维护测试号码、激活/暂停/恢复环境以及发送页面单号码 UAT。唯一例外是已经激活受控联调后，应用可用有效 `X-Api-Key` 调用 `/api/v1/messages/uat-send` 验证真实 API 接入链路；该入口不是生命周期控制面，也不能安装凭据、管理号码或改变激活状态。现有底层固定 wrapper 仅供 `vendor-control-agent` 调用和 root 受控应急，不是日常交互入口，也不接受页面传入任意命令、路径、环境变量或 Compose 参数。

真正接触正式 Key、测试手机号或远端服务器前，操作者必须对真实激活和首条短信**再次明确授权**。在授权前只能发布受控工具、运行静态门禁和 Mock G2，不得激活真实模式，也不得发短信。

以下任一条件出现立即停止：工作树或 CI/G2 未通过；迁移不是 expand-only；数据库存在 submitting、retrying 或 uncertain 活跃分片；active 测试号码为空；测试号码 HMAC 索引未覆盖全部保留版本；agent heartbeat 过期；加密 checkpoint 失败；GetBalance 非成功；当日账本不是零；任何 critical pause；服务包含 mock-vendor；证据可能包含 PII 或凭据元数据。

## 单机主机前置条件

所有目录、socket 和状态文件由 root 控制，不接受替代路径或符号链接：

| 对象 | 固定位置 | owner/mode |
|---|---|---|
| 测试主机 marker | `/etc/sms-platform/test-host` | root，`0400` 或 `0600` |
| 环境 marker | `/etc/sms-platform/test-environment` | root，`0400` 或 `0600` |
| 联调状态目录 | `/var/lib/sms-platform/vendor-test` | `root:10001`，`0710` |
| agent 状态投影 | `/var/lib/sms-platform/vendor-test/control-state.json` | `root:10001`，`0640` |
| 版本化凭据目录 | `/var/lib/sms-platform/vendor-test/credentials` | root，`0700`；generation 文件 `0600` |
| agent socket | `/run/sms-platform/vendor-control/vendor-control.sock` | `root:10001`，`0660` |
| 激活证据目录 | `/var/lib/sms-platform/vendor-test/evidence` | root，`0700` |
| 快速更新状态目录 | `/var/lib/sms-platform/test-updates` | root，`0700` |
| 加密备份配置 | `/etc/sms-platform/test-update-backup.json` | root，`0400` 或 `0600` |
| 加密备份密钥 | `/etc/sms-platform/test-update-backup-key` | root，`0400` 或 `0600` |

测试号码不再写宿主 allowlist 文件。PostgreSQL 的 `vendor_test_recipient` 是唯一号码事实源，每个号码只持久化 `phone_enc`、`phone_hmac`、`phone_mask` 与 `key_version`；`vendor_test_recipient_hmac_alias` 只保存不可解密的 `hmac_key_version/hmac_digest` 索引投影。页面列表只返回备注和掩码。API 使用 `10001:10001`，realtime worker 使用主身份 `10001:10002` 并仅以补充组 10001 读取 socket/状态文件；agent 按主 UID/GID 与 operation 校验，worker 只允许 `health/status`，不能执行安装、轮换、激活、暂停或恢复。两者均不能读取 root 凭据 generation 或获得 Docker Socket。

主机需预先配置固定 HTTPS 厂商 origin、可验证的 TLS/DNS、加密 checkpoint 目录和数据库连接。出口 IP **默认按已报备处理**；不为了验证报备而发探测短信。收到 1010 时按下文停发并报告。

### 从纯 Mock 首次准备控制台

发布受控代码和 expand-only 迁移后，先保持纯 Mock：项目根 `.env` 必须是 root `0600`，并显式包含 `DEBUG=1`、`AUTH_MOCK=1`、`VENDOR_MOCK=1`、`VENDOR_BASE_URL=http://mock-vendor:9028`、`COMPOSE_PROFILES=dev`。此阶段不得写 marker、安装凭据、登记号码或调用运营商。

旧测试环境若仍有 `.env.example` 的行尾说明，或遗留的 `LDAP_SERVER`、`LDAP_BASE_DN`、`LDAP_BIND_DN`、`LDAP_USER_SEARCH_FILTER`、LDAP timeout、`BOOTSTRAP_ADMIN_USERS` 键，pre-live 门禁会在证明固定五项仍精确为纯 Mock 后，原子删除这些已失效 Provider 键并把行尾说明规范化为严格格式。精确命中的废弃键会整行删除而不再解释旧值；该协调不改变任何保留配置值。未知键的非严格语法、任何重复键、凭据键或非 Mock 值仍立即拒绝且不写文件。

快速更新/临时 HTTPS 共用的 host-control 安装器会在固定 lifecycle lock 内一次性准备上表中的加密备份配置和密钥，锁冲突时不修改 checkpoint：密钥只在服务器本机随机生成，后续重装原样保留。config-only 必须阻断；唯一允许的 key-only 状态是 key 权限、长度、单链接和 inode 均安全且 checkpoint 目录为空，此时只补 config，并在提交前后复验同一 inode。config 原子出现是提交边界；其后的目录落盘或复核失败仍使当次安装失败，下一次仅可在完整复验 checkpoint、config/key 与 inode，并重新 `fsync` 权威目录后接受。权限/长度/硬链接异常、中断 checkpoint，或目录已有内容但权威文件不全时都必须阻断；不得用重新生成密钥的方式跳过，因为这会破坏既有密文 checkpoint 的恢复能力。

由 root 使用专用的 development host 配置和同一已审核 commit 的固定 unit 完成一次性 bootstrap；禁止现场编辑 `ExecStart`、能力集或读写路径：

```bash
sudo install -d -m 0700 /etc/sms-platform
sudo install -m 0600 deploy/systemd/compose.vendor-test.env.example \
  /etc/sms-platform/compose.env
sudo install -m 0644 deploy/systemd/vendor-control-agent.service \
  /etc/systemd/system/vendor-control-agent.service
sudo systemd-analyze verify /etc/systemd/system/vendor-control-agent.service
sudo /usr/bin/env \
  SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  /usr/local/sbin/sms-compose vendor-test bootstrap
sudo systemctl restart sms-platform.service
sudo systemctl is-active vendor-control-agent.service
sudo stat -c '%U:%G %a %F' \
  /run/sms-platform/vendor-control/vendor-control.sock \
  /var/lib/sms-platform/vendor-test/control-state.json
```

`vendor-test bootstrap` 在 lifecycle lock 内只做三件事：验证 unit 与仓库版本完全一致、确认仍是上述纯 Mock 配置、准备固定 state/socket 目录并启动 agent。它不修改项目根 `.env`，不创建 marker，不接收动态路径，也不启动真实出口。**不得在 bootstrap 中输入正式 Key 或测试手机号**。随后重启 `sms-platform.service`，让 API/worker 通过 `compose.vendor-test.env.example` 中的固定只读挂载看到 agent state/socket；页面首次应显示 `setup_required`，业务发送仍由 Mock 处理。

bootstrap 任一步失败即停止；不得手工伪造 `control-state.json`、放宽 owner/mode，或临时把 agent socket 改挂到仓库空目录。期望 socket 为 `root:10001 660 socket`，状态投影为 `root:10001 640 regular file`，且 agent heartbeat 持续刷新。检查只读取 owner、mode、类型和安全状态字段；不得读取凭据 generation 内容、大小、摘要或哈希。API/worker 只读状态投影，主机 Docker Socket 不得挂载进任何业务容器。

agent 对根 `.env` 与 `/etc/sms-platform/test-environment` 的更新使用同目录临时文件、`fsync` 和 `os.replace`，因此 systemd 必须允许这两个父目录完成原子替换；激活和轮换的密文 checkpoint 只写固定 `/var/lib/sms-platform/test-backups`。固定 unit 同时把项目源码、Git 元数据以及 `/etc/sms-platform/compose.env`、生产/冷备环境文件、测试主机 marker、备份配置和备份密钥以更具体的 `ReadOnlyPaths` 重新挂成只读，`deploy/secrets` 也保持只读；不得只把现有 `.env` 文件本身设为可写（会在创建 sibling temp 时以 `EROFS` 失败），也不得删掉这些更具体的只读边界。安装或升级 unit 后必须执行 `systemd-analyze verify`，并验证 agent 持续 active 且 restart 计数不增长。

版本化凭据根目录不得进入备份、恢复包、发布包或快速更新归档；`/var/lib/sms-platform/vendor-test/credentials` 只能在本机由 root agent 原子维护。数据库与 volume 的备份/恢复不能覆盖、导出或删除它，代码回退也不得切换 generation。需要灾难恢复时必须在新主机重新从系统配置页密封安装正式凭据，不得从普通备份复制。

bootstrap 完成后保持 `setup_required`，页面安装凭据后才进入 inactive；任何阶段都不自动启动真实发送，也不得由 unit、迁移、发布脚本或健康检查自动激活。状态满足全部条件后，只能由管理员在页面手工激活；首次真实激活与首条短信仍分别等待再次明确授权。

## 手机临时 HTTPS 安全入口

普通 `http://<server>:18080` 不是浏览器安全上下文。该 HTTP 入口的正式凭据对话框必须隐藏
当前密码、SecretName、SecretKey，并提示操作者在 ChatGPT 中发送“打开正式凭据安全入口”。
不得降级为明文提交、纯 JavaScript 替代加密、聊天传 Key 或服务器 TTY 输入。

测试环境使用 Cloudflare Quick Tunnel 提供按需 HTTPS；该能力仅限开发测试、无 SLA，
URL 为随机 `https://<label>.trycloudflare.com`，每次最多 15 分钟。流量会经过 Cloudflare
边缘，但正式 Key 仍由浏览器使用 agent 的单次公钥额外密封。隧道进程使用 DynamicUser，
只连接固定回环 Web，不读取 Docker Socket、运行 secrets、PostgreSQL、凭据 generation
或测试号码。

cloudflared 锁定为 `2026.7.2`，linux-amd64 SHA-256 为
`ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd`。二进制由
Codex/运维在本地从官方 release 获取并核验后上传；服务器不自行下载。源码必须由服务器
已取得的目标 Git object 解包到固定 root-owned `0700` staging，不接受普通上传目录或当前
可变 checkout。主机固定 `/usr/bin/python3` 必须为 3.11 以上并可导入 `cryptography`。
完整一次性安装命令和固定资产列表见 `deploy/README.md`「一次性主机安装」；安装器调用必须
包含以下三个固定输入：

```bash
SOURCE_ROOT=/var/lib/sms-platform/test-secure-access-bootstrap/source-<40位目标SHA>
sudo /usr/bin/env \
  PYTHONNOUSERSITE=1 \
  "PYTHONPATH=$SOURCE_ROOT/deploy/scripts" \
  /usr/bin/python3 \
  "$SOURCE_ROOT/deploy/scripts/install_test_secure_access.py" \
  --cloudflared-file \
    /var/lib/sms-platform/test-secure-access-bootstrap/cloudflared-linux-amd64 \
  --source-root \
    "$SOURCE_ROOT" \
  --source-commit <40位目标SHA>
sudo systemd-analyze verify \
  /etc/systemd/system/sms-platform-test-secure-access.service
sudo /usr/bin/env \
  SMS_SECRETS_MODE=development \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH=/usr/local/libexec/sms-platform/test-secure-access \
  /usr/bin/python3 \
  /usr/local/libexec/sms-platform/test-secure-access/test_secure_access_manager.py status
```

安装器逐资产核对目标 Git blob，验真完成后才导入主机模块，随后把 unit、contract、
runtime、manager、manifest 和 test-update bootstrap wrapper 安装到 root-owned 固定目录，
并创建独立测试主机 marker。为支持非特权 `DynamicUser`，安装器只为安全的固定资产目录链
补 known-path traverse 位（如 `0750→0751`），不增加目录 read/list 权限；不创建真实联调
激活 marker，也不自动启动 unit。日常由 Codex
执行固定动作。上面的首装 status 直接走固定 manager，避免旧 checkout 尚无
`secure-access` 子命令；应用更新完成后才走以下日常 wrapper：

```bash
sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access start
sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access status
sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access stop
```

start 成功后操作者在手机打开返回的 HTTPS URL、重新登录，再进入 `/configs`。页面应满足
`isSecureContext=true`、`crypto.subtle` 可用并显示既有密封表单。只有操作者本人输入正式
Key；Codex 和自动化只验证表面，不读取、代填或提交。安装成功也不登记测试号码、不激活
真实联调、不发送 UAT。新随机域名有 5 秒 DNS 传播等待和最长 60 秒 HTTPS 准备窗口；
只有该窗口耗尽仍不可达才失败关闭。

stop 或 15 分钟硬时限后，status 必须为 inactive，旧 URL 不可用，且无 cloudflared 进程、
额外监听和 `/run/sms-platform-test-secure-access` 状态残留。static unit 停止后可能已被
systemd 卸载，此时 `reset-failed` 返回非零不代表 stop 失败；管理器仍必须以最终
inactive/unknown 为成功硬判据。失败只保持入口关闭，不回退 HTTP 凭据表单。

## 页面安装凭据

管理员打开「真实联调」页后，必须使用当前登录所选的 local/AD Provider 完成二次认证。认证失败不得切换 Provider 或自动回退；成功后签发的 token 有效期 5 分钟、绑定账号/JWT/IP/操作类型，且是**单用途**、单次消费。

页面凭据对话框只允许 SecretName/SecretKey 在组件局部易失内存中短暂存在：

1. API 创建 120 秒、单次使用的 seal session，并把当前操作类型和登录人一并交给 root agent；响应只返回临时 RSA 公钥、session id、过期时间和 agent 生成的 AAD。
2. 浏览器使用 WebCrypto 生成 AES-256-GCM key，以 agent 返回的 AAD 加密两个长度限定字段，再用 RSA-OAEP 包装 AES key。AAD 同时绑定 session id、操作类型、登录人和过期时间，跨操作、跨账号或替换过期时间都必须拒绝。
3. 普通 API 只接收并原样转发密文封装，禁止接收明文 SecretName/SecretKey，也不得写数据库、队列、审计或日志。
4. `vendor-control-agent` 在 root 边界验证同一操作类型和登录人、消费 session 后解密。首次安装原子写入 active generation；轮换先写 pending generation，不直接切换 active。
5. 无论成功或失败，页面在 `finally` 清除输入值和临时 key material；不得回显或展示密钥值、长度、摘要、哈希、前缀或任何派生信息。

禁止把凭据放入 Pinia、localStorage、sessionStorage、IndexedDB、Service Worker cache、URL、浏览器错误报告或分析工具。状态接口只返回 `configured`、generation 状态与安装时间等安全投影。

已配置环境的页面轮换必须走完整事务：取得 lifecycle lock → 读取并保留既有 manual/critical/daily pause，若无暂停才创建本次 activation pause → 停止发送进程与 Outbox dispatcher → 再次确认 submitting/retrying/uncertain 活跃计数全部为零 → 创建加密 checkpoint → 在 root 私有目录持久化只含 previous/new generation 名称与 phase 的 `rotation-state.json` → 原子切换 pending generation → 重新生成运行时 secrets → 校验 Compose 并强制重建 API、三个 worker、Outbox dispatcher 与 beat → 只用 GetBalance 探测 → 成功后提交事务并只清除本次轮换自己创建的 activation pause。事务文件与 pending 指针必须保留到新运行态探针成功；文件中禁止出现凭据值或其派生信息。切换后的任一步失败，必须先写入独立的 rotation-failed critical 键，即使已有 manual pause 也不能被遮蔽；随后回切旧 generation、重新生成运行时 secrets、校验 Compose、重建旧服务并再次执行 GetBalance，旧运行态探针成功后才能清理事务。任一回退步骤失败都保留事务与 critical 暂停，最终仍报告本次轮换失败。不得只替换宿主凭据文件而让旧容器继续持有旧挂载。

agent 每次接收新的凭据 envelope 前都会先通过固定 wrapper 执行恢复：存在 `rotation-state.json` 时，在同一 lifecycle lock 内保持独立 critical 暂停、停止发送进程、回切 previous generation、重新生成运行时 secrets、校验 Compose、重建旧服务并用 GetBalance 验证，成功后才清理事务；只有不存在事务时，才把尚未切换的孤儿 pending generation 丢弃。事务未恢复时人工 resume 必须 fail closed，不能只凭一次探针清除暂停。页面只得到 `recovered/rotated` 安全状态。不得手工删除事务或 pending 指针、读取 generation 内容或跳过恢复直接重试。

## 页面登记测试号码

管理员在页面输入备注与一个 `^1\d{10}$` 号码。普通 API 仅在该次 HTTPS 请求内接收号码，立即调用统一 `CryptoService.protect_phone()` 写入 PostgreSQL；审计只写操作、数量和 recipient id，不写手机号或 HMAC。

列表只展示 `phone_mask` 与备注。停用或删除号码前必须确认没有活跃页面 UAT 或 queued/sending 测试批次；后者是 API UAT 的数据库维护租约，批次终态前号码维护必须返回忙碌。页面发送请求只接受 `recipient_id`，不接受 `phone`、`mobile`、手机号密文或 HMAC。受控应用 API UAT 接受一个标准 `mobiles` 明文号码，但该值只在请求内存中用于计算全版本 HMAC 并定位 active recipient；数据库返回的所有保留版本 digest 必须与当次输入完全一致，随后才转换为既有加密四元组，不进入 SQL、日志、审计或持久层。worker 加载分片时先取得与号码维护相同的 advisory xact lock，再次检查 active 后才释放；号码仅在现有发送流水线靠近厂商边界时受控解密到内存，任何异常、日志和证据都不得包含手机号或 HMAC。

新增或保留 HMAC key 版本后，管理员必须在同一页面对每个 active 测试号码点击“刷新索引”，按掩码提示重新输入同一号码。API 只用当次输入计算全部版本候选，并要求至少命中该 recipient 的既有索引后才原子替换；它不解密历史号码，也不回显候选。未覆盖当前全部保留版本时，真实 UAT fail-closed 返回安全错误，不得退化为只检查活动版本。黑名单检查使用全部候选，号码级频控统一使用最早仍保留版本的 digest，使普通发送与受保护 UAT 在轮换期间共享同一计数桶。

## 激活、暂停与恢复

凭据 configured、至少一个 active recipient、agent heartbeat 新鲜并且所有安全检查满足时，页面显示 `status=inactive`。管理员通过二次认证后点击激活，API 返回 `operation_id`；刷新页面只按 operation id 恢复非敏感进度。

`vendor-control-agent` 在同一 lifecycle lock 内严格执行：暂停 realtime/bulk → 停止发送 worker → 确认活跃分片为零 → 创建只含密文的 checkpoint → 校验凭据和 active recipient 数 → 写 marker 与 live dotenv → 移除 mock-vendor → 校验 Compose → 启动所需基础服务 → agent 自身执行 GetBalance → 确认上海自然日账本为零 → 启动发送 worker → 仅清除本次 activation 自己持有的 pause → 写无 PII 证据。API 不挂载或读取正式厂商凭据。成功后页面显示 `status=controlled`。

任何一步失败都保持发送暂停，不自动 restore、不回退 Mock，也不清库、不删除 volume。checkpoint 是恢复前的人工决策依据，不是自动回滚开关。agent 每 10 秒写安全状态投影；API/worker 发现 heartbeat 超过 30 秒即 fail closed，并设置独立 critical pause。

GetBalance 是唯一允许的无消费预检。GetReport 与 GetReply 具有“拉走即消费”语义，严禁作为连通性探针、健康检查或 activation 预检。

普通暂停可以直接执行；恢复 critical pause 必须重新二次认证、根因已修复且 GetBalance 成功。manual、daily 与 rotation-failed/agent-stale critical 原因分层保存；清除 critical 只能删除 critical 层，必须保留原有 manual 或 daily pause。任何未完成的凭据轮换事务都必须先完成受控恢复，禁止直接 resume。

## 页面清空联调设置

`/configs` 的「真实联调」页是清空联调设置的唯一入口。该动作只在已配置凭据的 `inactive` 状态且没有暂停投影时允许创建。`setup_required` 不允许创建新的 reset operation：若此前的 reset 已清除凭据但尚未完成号码终结，后端只按原 operation id 与同一 root journal 自动对账续跑，页面仅恢复该 operation 的非敏感进度，不再要求操作者提交第二次 reset。controlled、blocked、setup_required 或任意 pause 时动作不显示且不得执行；仍处于可清空状态但存在运行中 operation 时动作仍显示但禁用，不得重复提交。

管理员必须先阅读页面上的删除与保留边界，完成当前 Provider 二次认证，并精确输入“清空联调设置”。Provider 不得自动切换或回退；二次认证签发的单用途凭证只绑定本次 reset，普通 API 随后只把该凭证提交给 reset 接口，不创建 seal session，也不接收凭据、号码或确认短语。

reset 只删除全部凭据 generation（包含 root credential store 与 runtime generations 中的正式厂商凭据副本）与全部加密测试收件人，包括相关安全索引投影。它保留管理员账号、短信业务数据和审计记录，保留当日 UAT 用量与 uncertain 占额，并保留数据库、Docker volume 和运行态目录。受保护的持久对象明确包括 PostgreSQL、Docker volume 和非厂商 secret，运行态根目录本身也不删除。它不是系统初始化，不得重建数据库、清理 volume、删除运行态目录或重置当日安全账本。

root agent 在持久化 `reset_authorized` 后先幂等清空 credential store，再通过零参数固定 wrapper `vendor-test reset-runtime` 进入与 release 相同的 lifecycle lock。编排器生成只含固定 revocation tombstone 的新 runtime generation；tombstone 不读取、不包含也不派生自旧 Key。随后固定停止并删除 worker-realtime、worker-bulk 两个 vendor-secret reader，再从新 generation 重建并执行无输出内容探测；只有两个 reader 全部通过后才清理 stale generations。API 不再是 vendor-secret reader。切换撤销 generation 后不得回切旧 runtime generation，也不得让已删除的旧 reader 在失败重放中复活。

只有 credential store 已未配置、runtime 与三个 reader 的当次探测均已通过、旧 generations 已清理，agent 才把 root journal 推进到 durable `runtime_revoked` 并最终成功。任一步失败或进程中断都保持 journal `running`，不把失败缓存成不可恢复终态；只允许原 operation id 重放并从实际文件/容器状态继续。锁与 release 冲突时在 Docker 和 runtime 修改前 fail closed。错误只返回固定安全状态，不输出 Key 值、长度、前缀、摘要、哈希、generation 名称或子进程输出。

操作提交后，页面只按 operation id 跟踪非敏感进度，真实出口始终保持关闭。成功时刷新状态与收件人列表，最终安全状态必须是 `setup_required`，随后凭据安装、测试号码登记和激活仍须分别重新走本手册的受控页面流程，系统不得自动激活或发送。

失败时必须按事实提示部分设置可能已经清理，不得承诺回退或“保持原状”。页面保留原 operation id 的安全状态并刷新当前投影；原操作仍为非终态时，后端只能用同一 operation id 与 root journal 继续对账，禁止从通用 setup_required 投影创建新操作。若原操作已进入失败终态，操作者应继续保持真实出口关闭，根据安全代码完成 agent/journal 对账与故障处置；不得手工恢复 generation、测试收件人或状态投影，也不得借失败恢复清除当日 UAT、uncertain、暂停或审计事实。

清空联调设置是独立的高风险管理流程，不得夹带到快速更新，也不得夹带管理员初始化；更新脚本、发布脚本、bootstrap、迁移和健康检查都不得自动调用 reset。快速更新继续保留数据库、Docker volume 与运行态目录，管理员初始化仍只允许空系统按独立流程执行。

## 100 个计费条硬上限

上限按 Asia/Shanghai 自然日计算，是最终内容（签名与营销退订语均已计入）的 **100 个计费条**，不是 API 请求数、批次数或手机号数。多条短信按计费公式累加，发送前由 PostgreSQL 行锁原子预留；并发请求也不能越过 100。

账本总占用为 `in_flight + confirmed + uncertain`。明确的参数拒绝可释放预留；HTTP 超时或网络异常表示结果未知，分片进入 uncertain 并继续占用当日额度，严禁自动重发，也不得在同日手工清零。达到上限时只产生隔离的 daily pause，它在下一个上海自然日自动过期；不能借此清除 critical pause。

1010 是 IP 校验失败：必须 crit 告警并暂停双队列，停止后续真实发送，保留错误码、时间和无 PII 关联 ID，再告知操作者核对出口报备。1010、鉴权、余额或账号状态形成的 **critical pause** 只能在根因修复、GetBalance 成功并人工复核后解除；**daily pause** 仅因 100 条额度自动到期，两者不得相互覆盖。

## 页面真实 UAT 顺序

完成再次授权后才按最小真实流量逐项推进：

1. 页面确认 agent、凭据、recipient、迁移、worker 与 activation 证据正常，且 mock-vendor 不存在。
2. 从控制台选择一个 active recipient、enabled app、类别/模板/内容，预览最终计费条后发送；每次只允许一个号码。
3. 等待现有 poll_report 正常消费并核对回执；不手工调用 GetReport。
4. 经明确需要再验证模板、验证码打码、上行回复和回调，每项一次，累计占用始终远低于 100。
5. 任一异常先暂停和取证，不做故障注入，不通过撤销 IP 白名单制造 1010，也不为“测满”发送 100 条。

live-test 模式下，现有普通 API/Web 发送入口一律返回 `VENDOR_TEST_CONSOLE_ONLY`；页面单号码 UAT 继续复用现有类别、模板、计费、营销同意、OTP 打码、pipeline、`zhihui.py`、报告/回复 poller 和 callback。应用侧真实发送只允许使用下节的窄化 API UAT，不得改用普通 `/send`。

## 受控应用 API UAT

`POST /api/v1/messages/uat-send` 仅用于验证 `X-Api-Key → API → pipeline → queue → carrier` 真实接入链路。调用前必须确认页面仍显示受控联调中、目标应用已启用且允许通知、目标号码仍为 active，并对本次真实短信再次明确授权。

请求合同固定为：仅通知（`category=notice`）、一个已登记号码、直接内容、可选签名和必填 `biz_id`。`biz_id` 为同一应用 24 小时幂等键，长度 1–32；同一个测试动作重试必须复用原值。模板、定时、验证码、营销、第二个号码和任何额外字段都返回 `INVALID_PARAM`。所有调用先消费应用每分钟限流并校验通知权限，未获授权的 Key 不进入号码 HMAC 查询；控制状态损坏或过期会用 Redis 原子命令同时写入两个独立 agent-stale critical pause 键，写入未确认则返回 `CONTROL_AGENT_PAUSE_UNAVAILABLE` 并保持发送关闭。入口继续受应用/部门配额、每日 100 个计费条、uncertain 占额和所有 critical/daily pause 约束；HTTP 超时或网络异常严禁自动重发。

fish 终端使用下列纯标准库脚本。它只接受 `https://<临时 HTTPS 地址>` 且域名必须为当前
Quick Tunnel 的 `*.trycloudflare.com`；TLS 使用系统默认 CA 严格校验。Key 与完整手机号均由
`getpass.getpass()` 在 Python 进程内读取，不展开到 shell/curl argv，不写命令历史或文件。脚本拒绝所有 HTTP 重定向，避免 `X-Api-Key` 被带往其他地址；发送前会显示非敏感 `biz_id`，若响应超时只能复用该值重试：

```fish
python3 -c '
import getpass
import json
import secrets
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

origin = input("临时 HTTPS 地址（https://<临时 HTTPS 地址>）: ").strip().rstrip("/")
parsed = urlsplit(origin)
hostname = (parsed.hostname or "").casefold()
if (
    parsed.scheme != "https"
    or not hostname.endswith(".trycloudflare.com")
    or parsed.port not in (None, 443)
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in ("", "/")
    or parsed.query
    or parsed.fragment
):
    raise SystemExit("拒绝：必须输入当前 *.trycloudflare.com 的纯 HTTPS 地址")

api_key = getpass.getpass("API Key: ")
phone = getpass.getpass("已登记测试手机号: ")
suggested_biz_id = "api-uat-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
biz_id = input("biz_id（首次回车生成；超时重试必须粘贴原值）: ").strip() or suggested_biz_id
if not 1 <= len(biz_id) <= 32:
    raise SystemExit("拒绝：biz_id 长度必须为 1–32")
print("本次 biz_id:", biz_id, "；请求超时也必须复用该值，禁止重新生成")
body = json.dumps(
    {
        "category": "notice",
        "mobiles": [phone],
        "content": "API渠道真实联调测试，请忽略。",
        "biz_id": biz_id,
    },
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
url = urlunsplit(("https", parsed.netloc, "/api/v1/messages/uat-send", "", ""))
request = Request(
    url,
    data=body,
    headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
    method="POST",
)
try:
    with build_opener(NoRedirect).open(request, timeout=15) as response:
        status = response.status
        response_body = response.read(4097)
except HTTPError as error:
    status = error.code
    response_body = error.read(4097)
except URLError:
    raise SystemExit("HTTPS 请求失败；未自动重试，先检查临时入口和网络") from None
finally:
    api_key = phone = ""
if len(response_body) > 4096:
    raise SystemExit("响应过大，已拒绝显示")
print("HTTP", status)
print(response_body.decode("utf-8", errors="replace"))
'
```

成功受理返回 `200`、`batch_no`、`idempotent`、`accepted=1` 和预计计费信息；这表示已进入真实发送队列，不等于最终送达。随后只通过平台批次状态和正常 poll_report 观察结果，不手工调用 GetReport。未登记号码返回无 PII 的 `FORBIDDEN`；控制状态不新鲜或已暂停返回 `CONTROL_AGENT_UNAVAILABLE`；不得为了得到成功响应而关闭门禁。

执行记录只保留状态、数量、计费条、厂商整数错误码和内部关联 ID；不得记录正文、手机号、手机号密文/HMAC、请求体或任何 secret 元数据。1010 只展示安全错误码，不展示厂商原始消息或请求体。

## 联调期间更新与受控应急

settings、厂商适配、发送 worker、计费、credential/recipient/额度、Compose、agent、
activation 和 update manager 均为 high-risk。共享分类会让这些 commit 在 CI 执行完整
G2；快速更新入口必须先验证目标 commit 的精确 `ci-gate=success`，但不重复测试。远端仍严格暂停并拒绝
submitting/retrying/uncertain；只有实际迁移才创建密文 checkpoint 并校验 expand-only。
控制面首次自更新前还必须先安装同 commit 的 root-owned host-control bootstrap。详见
[测试环境快速更新手册](test-fast-update.md)。

本次临时 HTTPS 应用变更仍先完成提交、推送与合并，再按需通过
`scripts/test_update.sh apply --ref origin/main`；只有服务器返回 `state=verified` 并完成
HTTP/HTTPS 表面验收才算成功。快速更新与一次性主机安装分开执行，且都不自动启动隧道、
不安装或轮换正式 Key、不登记测试号码、不激活真实联调、不执行管理员初始化、不初始化
数据库。全程保留 PostgreSQL 数据库、Docker volume、凭据 generation、号码和运行态数据。

high-risk development 统一发布必须使用同一 `release_id` 完整执行并保留 `release-rollback/<release_id>`。driver 只有在 release status 为 `succeeded` 且 rollback 基线到候选之间的 agent 运行文件发生变化时，才执行固定 `sudo -- /usr/bin/systemctl restart vendor-control-agent.service`，并立即用 `systemctl is-active --quiet vendor-control-agent.service` 验证。restart 或 active 校验失败都必须失败关闭，停止公网探针与后续验收；修复后以同一 release 重试，不能留下新 API/journal 对旧 agent 进程。该动作只更新宿主进程代码，不安装 Key、不改测试号码、不激活真实联调，也不属于 ordinary quick update。

页面或 API 不可用时，先保持暂停并保存无 PII 状态；不得临时给 API sudo/root、挂载 Docker Socket，或直接编辑 active generation。底层固定 wrapper 仅允许 root 按已审计的受控应急手册执行固定 `status`、`pause` 等动作；恢复页面服务后仍以 PostgreSQL operation 与 agent journal 对账，禁止借应急路径发送真实短信。
