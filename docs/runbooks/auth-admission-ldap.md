# LDAP 第二阶段与共享出口准入

这份手册覆盖 #591、#592 的代码能力。MFA（#594）未纳入本轮；真实目录分布、来源粒度及生产容量仍须独立验收。本地合成结果不能关闭这两项环境验收。

## 配置边界

运行凭据仍只从原 Docker secrets 文件读取。新文件不包含密码，必须由部署操作者维护并以只读文件挂载到 API；普通配置页和请求不能指定目录 sink 或可信来源。宿主文件须在受控运行配置目录中，不能改写 Git tracked 文件或把真实地址提交到仓库。路径环境变量仅指定容器内挂载位置：

- `LDAP_TIMING_PROFILE_FILE`：目录批准的安全第二阶段合同。未配置时真实 LDAP 认证与连接测试失败关闭；未启用 AD 或 `AUTH_MOCK=1` 的系统不要求提供它。
- `AUTH_SOURCE_PROFILE_FILE`：来源审批文件。未配置时所有来源使用严格互联网策略；文件损坏、未知格式或网络重叠时失败关闭。正常到期的来源回到严格策略。

正式 Compose 已将专用目录只读挂到 API 的 `/run/auth-policy`；其他服务不挂载。`deploy/sms-compose` 在生产固定宿主目录 `/etc/sms-platform/auth-policy`，校验目录为 root:10001 / 0750、文件为 root:10001 / 0640 且无符号链接。上线前由已批准的部署流程准备目录（禁用 AD 且没有共享来源时可以为空）及 `ldap-timing.json`、`sources.json`，并在受控环境文件中设置对应容器路径。无需也禁止 `COMPOSE_FILE` 或 `SMS_AUTH_POLICY_DIR` 覆盖。开发和隔离测试使用 `deploy/auth-policy`，其中实际 JSON 被 Git 忽略。本轮没有创建或修改宿主生产目录。

目录文件结构示例（仅保留虚构值；不得直接用于实际目录）：

```json
{
  "version": 1,
  "server": "ldaps://directory.example.invalid:636",
  "service_bind_dn": "CN=service,DC=example,DC=invalid",
  "sink_dn": "CN=auth-rejection,OU=NonAccounts,DC=example,DC=invalid",
  "safety_contract": "non_account_rejection",
  "approval_ref": "CHANGE_EXAMPLE",
  "expires_at": "2027-01-01T00:00:00+08:00"
}
```

`safety_contract` 是目录负责人必须证明的事实：该目标不是业务用户/服务账号，不会因随机无效输入锁定真实账号，且明确返回 LDAP 49。代码无法从 DN 名称验证这些目录属性。不能用“禁用真实账号”或随机真实 DN 代替。每次无匹配/非唯一搜索使用新的内存随机输入；不使用请求中的用户名、密码或候选 DN。连接复用原精确目标、CA、无 referral、字节限制和总时限。异常成功、非 49 拒绝、超时及配置失效返回 503，不计入候选账号失败。就绪检查只核对合同，不执行错误 Bind。

来源文件示例：

```json
{
  "version": 1,
  "profiles": [{
    "cidr": "192.0.2.9/32",
    "approval_ref": "CHANGE_EXAMPLE",
    "expires_at": "2027-01-01T00:00:00+08:00"
  }]
}
```

最多 32 个不重叠网段，IPv4 至少 /28、IPv6 至少 /120；通常应使用 /32 或 /128。世界网段、组播、未指定地址、IPv4-mapped 配置被拒绝。请求中的 mapped 地址先规范化为 IPv4，NAT64 不自行推断，客户端 Header/Cookie 不能选择 Profile。审批证据应包含网络团队确认的最终 `trusted_client_ip()` 粒度、规模、容量测试、到期复核责任及回退窗口，真实明细保存在私有变更材料中。

## 阈值与结算

管理员通过现有系统参数入口维护 `sys_config.auth_admission_policy` JSON。默认值为：

```json
{"version":1,"shared_burst":100,"shared_window":200,"shared_refill_ms":1000,"global_burst":8,"global_refill_ms":250,"global_concurrent":4,"source_concurrent":2}
```

共享来源上界：burst 200、window 500、refill 不快于 250ms；全局上界：burst 16、refill 不快于 100ms、并发 16；单来源并发最多 8 且不能超过全局。公网仍为 5 次突发、每 15 秒恢复 1 次、300 秒窗口最多 20 次。提高阈值不增加执行器线程：本地登录与重认证保持各自独立的 1 worker/2 pending 池，LDAP 为 4/8；Redis 分别限制跨实例登录 2、重认证 2、目录 8 个在途操作，仍共同消耗全局工作预算。

后台每 5 秒读取一次策略，最长使用已验证快照 15 秒。`updated_at` 微秒值由单调触发器作为 revision；Redis CAS 检查 revision 与完整配置摘要。来源文件调整时同时通过已审计配置 API 保存策略（可保存相同值）推进 revision。全部 API 实例必须使用一致文件；旧实例不能把旧策略覆盖到 Redis，配置冲突时失败关闭。

全球工作桶不因成功、退款或策略发布重置；速率变更先按旧速率结算旧时间段。可信共享来源按每次内部 reservation 预留额度，只有权威绑定且完整会话签发、或有效重认证完成后才单次返还。首次改密挑战、身份冲突、旧 Family 撤销/签发失败不退款；旧世代或旧窗口不能给新状态加额度。独立账号失败、IP ban 与审计保持原合同。

全局并发以实际工作完成为准。协程取消/超时不会立即释放仍在运行的线程；正常 API 停机先等待这些线程及释放任务，再关闭 Redis。遗留且已超过 120 秒的槽视为未知状态，保持 503，不能按 TTL 自动当成空闲。已发布过策略的进程发现全部控制键丢失也失败关闭；从未发布的新进程只允许完全空状态从零工作额度初始化。进程崩溃或 Redis 状态损坏需要保留事实并按独立批准的停流/恢复流程处理，重建或启动替代进程前必须确认旧 API 全部停止。本轮不提供删除并重建控制键的运维入口，不得直接删除 `auth:*` 或把缺失状态改成满桶。

浏览器仅对服务端明确标记 `auth_admission_retry=true` 的 429 等待重试，保留原会话 generation 和 tab binding，最多 30 次，累计等待预算 30 秒。等待可被页面取消；密码错误、IP ban、503、超时与结果未知的请求不重试。等待结束仍可能因容量不足失败；实际 SLO 需要环境测量。

## 验证与上线

1. 使用官方本地 Mock 与一次性 PG/Redis 入口验证迁移、真实运行角色、原 Lua、并发、退款重放、取消以及 LDAP 连接替身；不连接真实目录。
2. 在批准的测试 OU 和入口随机交错采样 `missing / wrong_password / correct / disabled / locked / unavailable`，覆盖 `cold_single / warm_single / cold_multi / warm_multi`。禁止把实际身份、DN、密码、证书或地址写入样本。
3. 使用离线工具 `python scripts/auth_timing_report.py --input <redacted.json> --output <report.json>`。样本仅接受 `{"version":1,"samples":[{"case":"missing","mode":"cold_single","duration_ms":123.4}, ...]}`。工具报告 P50/P90/P95/P99、标准差、方向无关秩 AUC 与 bootstrap 区间，不自动判定通过；样本功效、AUC 与绝对容差须预先批准，并比较多轮结果。绑定真实候选 SHA、入口和网络条件的脱敏证据由受控 UAT 流程保管。
4. 验证同一批准出口 50–100 个合法用户、少量错误密码混合、公网攻击、网络切换和 Redis 故障。记录真实 Argon2/LDAP 调用数、内存、并发、误拒与端到端 SLO；不能只用快速 Provider 替身证明容量。
5. 使用已有维护发布入口；本轮不部署、不执行服务器迁移、不启用真实 sink/来源，不修改真实凭据。回退保持严格策略或关闭受影响 AD，不能静默恢复快速缺失用户失败路径。数据库、卷和审计事实保留。

可观测指标包括 `auth_source_admit_total{profile,outcome}`、`auth_prehash_refund_total{profile,outcome}`、统一 LDAP 失败耗时 sum/count、sink 故障与 deadline 次数。Profile 只有 internet/shared；指标不含用户名、IP、DN、CIDR 或 token 标签。

## 非 MFA 失败信号与离线密码库

迁移 `0113_auth_spray_policy` 在保留现有容量值的同时加入喷洒阈值并推进策略 revision。
升级需按既有迁移发布入口协调 API 切换；旧二进制不接受新增字段，不能混跑或只回退旧镜像。
本次代码交付不执行迁移、服务器更新或密码库安装。

`auth_admission_policy` 新增 `spray_failures`（默认 12，范围 5–100）、
`spray_sources`（默认 4，范围 2–32，对双向不同主体位数生效）、
`spray_delay_ms`（默认 250，范围 50–1000）。达到阈值后失败响应延迟一个单位，
失败数达到两倍阈值后延迟两个单位，始终不超过 1000ms。Redis 故障或状态损坏时失败关闭。
窗口内只有匿名化近似行为证据；指标 `auth_password_spray_signal_total{risk_level}`
仅有 elevated/high/unavailable 标签，不含账号、IP 或凭据。它不是持久审计或外部通知。
采用固定桶和 32 位去重会发生碰撞或低估，不是精准攻击归因；固定窗口也不能覆盖任意低速攻击。
信号跨成功登录保留至 TTL，但不阻止正确凭据认证。MFA、挑战、风险新会话降权和新管理动作的
二次认证产品范围仍待确定；不把密码重复输入等同于独立因素。

生产在受控 auth-policy 目录安装 `password-corpus.json`（root:10001、0640、无符号链接），
并配置 `AUTH_PASSWORD_CORPUS_FILE=/run/auth-policy/password-corpus.json`。
文件是 JSON 对象，只允许 version（整数 1）、source_ref（脱敏版本引用）、expires_at
（带时区 ISO8601）、sha256（不重复的小写 SHA-256 hex 数组）四个字段。
文件最多 8 MiB、1–100000 条；摘要按原始 UTF-8 密码计算，不做大小写或 Unicode 改写。
条目应由运维在受控离线流程从批准的常见/泄露语料整理，记录来源、许可、覆盖范围、版本及到期日；
不可收集本系统的用户密码生成语料，不可把真实密码交给在线检查工具。
生产发布前必须提供真实批准语料并验证代表性命中与正常控制，测试的合成单条库不具备生产覆盖证明。
先原子替换完整文件，再重启 API 加载不可变快照；加载失败或到期会拒绝本地密码写入，
恢复有效文件并重启后才恢复。init-admin 在同一 API 容器文件边界加载库，检查失败不会生成管理员。
已有账号登录、刷新和 AD 校验不依赖语料；不得为了恢复开户/改密移除生产配置来绕过筛查。
