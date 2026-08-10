# 企业短信管理平台 v1.6 远程测试环境完整测试手册

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.1 |
| 编制日期 | 2026-07-30 |
| 系统版本 | 企业短信管理平台 v1.6 |
| 测试环境 | 动态；执行前必须读取 `scripts/test_update.sh status` |
| Web 入口 | `${TEST_BASE_URL}/login`（由测试负责人经受控渠道提供） |
| 测试时区 | Asia/Shanghai（UTC+08:00） |
| 文档状态 | 可执行基线 |

## 1. 文档说明

### 1.1 目的

本手册用于指导业务测试、接口测试和运维测试人员在已部署的远程测试服务器上完成全量验收。它给出统一环境、角色、数据、执行顺序、28 项核心 UAT、专项检查、证据规则、恢复动作和报告模板。

需求结论以 [PRD.md](../PRD.md) 为准，接口以 [openapi.yaml](../openapi.yaml) 为准，厂商
Mock 以 [vendor-api.md](vendor-api.md) 为准。历史自动验收证据保留在受限归档中，不随公开
快照发布，也不能替代本轮真人执行记录。

### 1.2 适用人员

| 角色 | 主要职责 |
|---|---|
| 测试负责人 | 制定轮次、分配账号、审批高风险用例、汇总结论 |
| 业务测试员 | 页面、角色权限、人工发送、审批、查询、报表和管理功能 |
| 接口测试员 | API 幂等、错误码、应用权限、Mock 故障和回调验签 |
| 运维测试员 | 容器、任务、日志、安全验收、性能和重启恢复 |
| 产品/业务代表 | 核对中文文案、流程、统计口径并签署 UAT |

### 1.3 测试范围

- 四角色登录、菜单可见性、路由保护和数据权限；
- 登录页加 17 个登录后页面，共 18 个页面；
- verify、notice、market 三类短信的受理、审批、调度、发送和查询；
- 模板、签名、应用、黑名单、敏感词、用户、参数、回调、运维和审计；
- 厂商 Mock 的成功、失败、限速、余额不足、超时、报告、回复和回调；
- 手机号保护、OTP 打码、审计隐私、授权解密、密文导出和回调安全；
- 桌面端、390 px 移动端、性能冒烟、任务心跳及恢复能力。

### 1.4 环境边界与非目标

环境状态不是静态文档事实。每轮测试前必须在已批准的本地连接配置下执行：

```bash
scripts/test_update.sh status
```

本次文档对齐时，配置的测试服务器为 `development-vendor-live` / `controlled`：认证仍为
开发 `AUTH_MOCK`，厂商侧是受控真实联调，不是 Mock。live-test 下普通 Web/API 发送入口
必须返回 `VENDOR_TEST_CONSOLE_ONLY`；只有 `/configs`「真实联调」页和
`POST /api/v1/messages/uat-send` 能在严格授权下向一个 active 已登记测试号码发送，
共同受每日最多 100 个计费条、uncertain 占额、应用限流和 critical pause 约束。该路径会
产生真实短信和实际计费，必须由授权人员在独立窗口执行。

CI、G2、故障注入、魔法号、Mock Send 计数和本文其余 Mock 用例仍只允许在隔离的
`AUTH_MOCK=1` / `VENDOR_MOCK=1` 环境执行，禁止直接套用到 controlled 服务器。以下项目
仍不在当前远程测试通过范围内：

- 真实 LDAP/AD 组映射、证书链与账号生命周期；
- 不受控制台/窄化 UAT API 约束的真实短信发送、生产流量和真实送达率；
- HTTPS/TLS/HSTS、生产域名和 WAF；
- 24 小时十万条长稳压测、真实主备切换和生产 RTO；
- 生产 24 件 secrets、正式告警渠道和发布审批。

上述项目必须在隔离预生产或生产演练中另行验收，不得用 Mock 或 controlled UAT 结果代替。

### 1.5 安全红线

- 禁止在截图、缺陷、聊天、工单或本手册中粘贴 API Key、JWT、Docker secrets、SSH 密码或私钥；
- Mock 用例只用批准虚拟号段；真实 UAT 只用控制台内 active 已登记的专用测试号码；
- 证据中手机号只保留平台 mask；不得导出、复制或上传明文号码列表；
- 不在浏览器控制台、shell trace、日志命令中打印认证头或 secret 文件；
- 普通测试员不得执行停服务、重置数据、故障注入、配置缩短或队列恢复；
- 所有临时参数和 Mock 状态必须在用例结束时恢复，即使用例失败也要恢复。

## 2. 测试环境

### 2.1 访问入口

| 项目 | 地址 | 对外状态 | 用途 |
|---|---|---|---|
| Web 登录 | `${TEST_BASE_URL}/login` | 按测试窗口受控开放 | 所有人工页面测试 |
| 存活检查 | `${TEST_BASE_URL}/livez` | 按测试窗口受控开放 | 仅证明 API 进程可响应 |
| 就绪检查 | `${TEST_BASE_URL}/readyz` | 按测试窗口受控开放 | 无敏感细节的接流判定 |
| Web API | `${TEST_BASE_URL}/api/v1` | 经 Nginx 同源代理 | 浏览器与受控接口测试 |
| API 直连端口 | 不公开 | 仅服务器回环 | 运维脚本与诊断 |
| 厂商 Mock 端口 | 不公开 | 仅隔离 Mock 环境回环 | 授权故障注入与断言 |

测试开始前应确认登录页与健康检查返回 HTTP 200；API 和 Mock 直连端口不可从公网访问，
这是正确的安全状态。controlled 模式不得暴露厂商端口或绕开
`vendor-control-agent`。若可通过公网直连 API/Mock/厂商，立即按 P0 安全缺陷上报并停止测试。

### 2.2 测试账号

四个账号共用该轮随机 Mock 密码。密码由运维从服务器 0600 `ldap_bind_password` secret 经受控渠道一次性提供给测试负责人，不得写入本手册、Issue、截图或聊天；每次疑似泄露都必须轮换。

| 用户名 | 角色 | 主要范围 |
|---|---|---|
| `admin01` | 管理员 | 全部页面、系统配置、用户、审计和运维 |
| `approver01` | 审批员 | 审批、授权查询、报表、模板和签名 |
| `operator01` | 操作员 | 人工发送、批次、回复、模板和签名 |
| `viewer01` | 查看员 | 本部门只读查询、回复和报表 |

切换角色前必须点击右上角“退出”，再打开新的隐私窗口登录；不得手工把一个用户的 JWT 复制给另一个用户。

### 2.3 测试应用

| 应用 | 类别 | 用途 |
|---|---|---|
| `app-iam` | verify | OTP、频控、实时队列、打码和异常告警 |
| `app-oa` | notice | 通知、幂等、定时、失败重发和回调 |
| `app-mkt` | market | 营销窗口、退订语、同意、审批和批量发送 |

应用 API Key 由测试负责人通过安全渠道临时发放。手册、截图和命令记录中统一写作 `<APP_API_KEY>`，执行后立即清除 shell 历史或临时会话。

### 2.4 运维访问边界

服务器登录方式沿用已批准的远程部署会话，不在仓库中复制主机用户名、密码或密钥。需要服务器权限的步骤必须由授权运维人员执行，并遵守以下规则：

```bash
sudo /usr/local/sbin/sms-compose ps
sudo systemctl status sms-platform.service --no-pager
curl -fsS http://127.0.0.1:<API回环端口>/livez
curl -fsS http://127.0.0.1:<API回环端口>/readyz
curl -fsS http://127.0.0.1:<MOCK回环端口>/_mock/state
```

回环端口由运维人员从受控部署配置或 `sms-compose ps` 的端口映射确认，不得通过读取或输出 secret 文件推断。普通测试人员只使用 18080 Web 入口。

### 2.5 测试版本登记

每轮开始前记录：

```text
测试轮次：
开始时间（+08:00）：
目标 Git commit：
服务器镜像 digest：
Web 地址：${TEST_BASE_URL}
执行负责人：
环境档位：从 status 原样登记（不得包含主机、凭据或号码）
已知限制：公网 HTTP、开发认证；厂商档位按 status 选择 Mock 或 controlled UAT；告警 log-sink
```

若目标 commit、镜像 digest 或数据库迁移版本无法确认，不执行全量 UAT，只执行健康检查并上报环境阻塞。

## 3. 测试准备

### 3.1 客户端矩阵

至少准备以下客户端，浏览器均使用当前受支持的稳定版本：

| 编号 | 终端 | 视口 | 浏览器 | 必测范围 |
|---|---|---:|---|---|
| C1 | Windows/macOS 桌面 | 1440×900 | Chrome | 全量业务与管理页面 |
| C2 | Windows 桌面 | 1366×768 | Edge | 核心流程与表格/抽屉 |
| C3 | macOS/iOS | 桌面或手机 | Safari | 登录、发送、查询、报表 |
| C4 | 手机或设备模拟 | 390×844 | Chrome/Safari | 18 页面导航、卡片布局和触控 |

浏览器缩放保持 100%。关闭会改写请求、注入脚本或自动翻译页面的扩展。

### 3.2 测试数据

测试负责人建立不含真实 PII 的数据台账，使用以下占位符：

| 占位符 | 含义 |
|---|---|
| `<测试手机号A>` | 通过 `^1\d{10}$` 校验的专用测试号 |
| `<测试手机号B>` | 与 A 不同的专用测试号 |
| `<失败魔法号>` | 从批准号池选择、前缀满足 `1990000****` 的 Mock 失败号 |
| `<无报告魔法号>` | 从批准号池选择、前缀满足 `1991000****` 的 Mock 无报告号 |
| `<RUN_ID>` | `UAT-日期-轮次-短随机串`，用于名称和 `biz_id` 去重 |
| `<APP_API_KEY>` | 安全渠道临时取得的应用 Key，不落文档 |

测试内容使用虚构文案，不出现真实客户姓名、身份证、合同号或业务秘密。verify 内容中的 OTP 使用随机 4–8 位数字，测试完成后不记录原值。

### 3.3 前置检查

- [ ] 登录页、`/livez` 和 `/readyz` 返回 HTTP 200；
- [ ] 已执行 `scripts/test_update.sh status` 并登记 vendor mode/status；
- [ ] Mock 专项确认 `VENDOR_MOCK=1`；controlled UAT 确认普通发送返回 `VENDOR_TEST_CONSOLE_ONLY`；
- [ ] 测试负责人已登记 commit、镜像和执行窗口；
- [ ] 四账号均处于未锁定状态；
- [ ] `app-iam`、`app-oa`、`app-mkt` 可见且状态正常；
- [ ] 测试号池、截图目录和缺陷系统已准备；
- [ ] 高风险用例已安排独立时间窗和运维人员；
- [ ] 本轮开始前已记录 sys_config、Mock 状态、队列和 beat 基线；
- [ ] 没有其他团队正在共用该环境执行性能或故障测试。

### 3.4 证据命名

证据文件名统一为：

```text
<轮次>_<用例编号>_<步骤序号>_<结果>_<+08时间>.<扩展名>
```

示例：`R1_UAT-11_S05_PASS_20260714-143000.png`。截图必须包含页面标题、关键状态和 +08:00 时间，但需遮蔽手机号、token、Key、响应原文中的敏感字段。日志只记录时间窗、服务名、错误码和安全摘要。

## 4. 测试管理规则

### 4.1 用例优先级

| 优先级 | 含义 | 执行要求 |
|---|---|---|
| P0 | 安全、重复发送、认证、状态机生命线 | 每轮必测，失败立即停止相关链路 |
| P1 | 核心业务闭环 | 每轮必测，失败阻断验收 |
| P2 | 管理、查询、报表和兼容性 | 全量轮次执行，冒烟轮次可抽样 |
| P3 | 易用性和视觉细节 | 发布前执行并登记改进项 |

### 4.2 缺陷等级

| 等级 | 判定示例 |
|---|---|
| Critical | 真实外呼、手机号/密钥泄露、uncertain 自动重发、越权解密、审计可改删 |
| Major | 核心流程不可完成、错误状态/计费/配额、角色越权、恢复失败 |
| Minor | 非核心功能异常、局部兼容问题、可绕过的交互问题 |
| Trivial | 文案、间距、非阻断视觉问题 |

### 4.3 中止条件

出现以下任一情况立即停止当前测试面：

- Mock 档位指向真实厂商，或 controlled 档位在批准入口/号码/预算之外产生真实短信；
- 日志、数据库、API 或证据出现明文手机号、Key、JWT 或 secret；
- Send 超时后系统自动重发；
- 同一 `biz_id` 产生两个实际发送批次；
- admin 以外角色访问管理/运维页面或 viewer 获得写权限；
- 临时参数、Mock 故障、队列或账号状态无法恢复；
- 健康检查连续 3 次失败或数据库/Redis 不健康；
- 与其他测试团队发生并发环境污染。

### 4.4 推荐执行顺序

1. 环境冒烟；
2. 四角色和 18 页面；
3. UAT-01、UAT-04、UAT-05～UAT-11、UAT-13～UAT-15、UAT-18～UAT-19、UAT-21～UAT-24、UAT-28；
4. 独立窗口执行账号锁定/IP 限流 UAT-02～UAT-03；
5. 运维窗口执行 UAT-12、UAT-16～UAT-17、UAT-20、UAT-25～UAT-27；
6. 安全、性能、兼容性与恢复专项；
7. 清理数据、复跑冒烟、汇总报告。

## 5. 环境冒烟

| 编号 | 检查 | 操作 | 预期 |
|---|---|---|---|
| SMK-01 | Web 可达 | 打开登录页 | HTTP 200，显示“青鸾 短信运营控制台”登录界面 |
| SMK-02 | API 存活与就绪 | 依次打开 `/livez`、`/readyz` | 均为 HTTP 200，响应不含 secret、DSN、组件名、堆栈或手机号 |
| SMK-03 | 未登录保护 | 直接打开 `/dashboard` | 跳转 `/login` |
| SMK-04 | 正常登录 | `viewer01` 正确登录 | 进入仪表盘，顶栏显示用户和角色 |
| SMK-05 | 退出 | 点击右上角“退出” | 回到登录页，刷新后不能恢复旧会话 |
| SMK-06 | 错误登录 | 使用错误密码 1 次 | 统一中文错误，不暴露账号是否存在或后端堆栈 |
| SMK-07 | 静态资源 | 打开开发者工具 Network | JS/CSS/字体均来自当前站点，无运行时外部 CDN |
| SMK-08 | 安全头 | 检查主页面响应 | 存在 CSP、Permissions-Policy、X-Content-Type-Options、X-Frame-Options、Referrer-Policy |
| SMK-09 | 运维健康 | 运维执行 `sms-compose ps` | 9 个测试栈容器处于预期运行/健康状态，migrate 正常退出 |
| SMK-10 | 公网边界 | 从测试终端探测 API/Mock 直连 | 不能公网直连，只能经 18080 同源代理访问 API |

冒烟任一 P0/P1 项失败，不继续全量 UAT。

## 6. 页面与角色测试

### 6.1 角色菜单矩阵

“可见”表示菜单存在且路由可进入；“隐藏”表示菜单不显示，直接输入 URL 也应回到仪表盘或返回 FORBIDDEN。

| 页面 | viewer01 | operator01 | approver01 | admin01 |
|---|---|---|---|---|
| 登录 | 公共 | 公共 | 公共 | 公共 |
| 仪表盘 | 可见 | 可见 | 可见 | 可见 |
| 统计报表 | 可见 | 可见 | 可见 | 可见 |
| 人工发送 | 隐藏 | 可见 | 隐藏 | 可见 |
| 审批中心 | 隐藏 | 隐藏 | 可见 | 可见 |
| 批次列表 | 可见 | 可见 | 可见 | 可见 |
| 号码搜索 | 可见 | 可见 | 可见 | 可见 |
| 上行回复 | 可见 | 可见 | 可见 | 可见 |
| 模板管理 | 隐藏 | 可见 | 可见 | 可见 |
| 签名管理 | 隐藏 | 可见 | 可见 | 可见 |
| 应用管理 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 黑名单 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 敏感词 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 用户与角色 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 系统参数 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 回调任务 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 运维中心 | 隐藏 | 隐藏 | 隐藏 | 可见 |
| 审计日志 | 隐藏 | 隐藏 | 隐藏 | 可见 |

### 6.2 逐页检查清单

| 页面 | 核心检查 |
|---|---|
| 登录 | 正确/错误登录、锁定提示、回车提交、密码不可明文显示、移动端不溢出 |
| 仪表盘 | 统计卡、余额趋势、告警、任务健康、空态、刷新与 +08:00 时间 |
| 统计报表 | 时间/应用/类别筛选、趋势图、成功率口径、分页/导出状态 |
| 人工发送 | 类别、应用、模板、签名、号码输入/导入、计费预估、营销同意、提交结果 |
| 审批中心 | 待办筛选、批次详情抽屉、通过/驳回、自审拒绝、重复审批冲突 |
| 批次列表 | 状态/应用/时间筛选、分页、详情抽屉、取消/改期/失败重发 |
| 号码搜索 | HMAC 精确查询、类别/状态筛选、分页、失败回报、mask、授权解密、时间线截断提示、无结果与越权 |
| 上行回复 | 回复列表、筛选、详情、退订加黑、号码 mask、空态 |
| 模板管理 | 新建、占位符与 var_specs、提交厂商、同步、状态和参数超长 |
| 签名管理 | 新建、提交厂商、同步状态、更新、空态和错误提示 |
| 应用管理 | 应用详情、类别权限、配额、频控覆盖、Key 轮换、callback URL 双检 |
| 黑名单 | 批量添加、重复项、删除、mask、分页和审计 |
| 敏感词 | 批量添加、block 策略、删除、命中提示和审计 |
| 用户与角色 | 搜索、数据范围、AD/人工角色、强制下线、不可隐藏提权 |
| 系统参数 | 类型校验、跨字段校验、保存提示、beat 重启提示、审计和恢复 |
| 回调任务 | 状态/应用/事件/批次号筛选、dead 总计、详情、手动重推、下次重试时间与错误摘要 |
| 运维中心 | 告警、任务、raw、uncertain、unmatched、手动触发和审计 |
| 审计日志 | 操作人/动作/对象/时间筛选、只读、载荷无手机号列表和 secret |

### 6.3 通用页面检查

每个页面均执行：

- 页面标题、面包屑和菜单高亮一致；
- 加载时有 Element Plus loading，空数据有中文空态；
- API 失败显示 `{code, message, detail}` 对应的中文提示，不出现裸 500；
- 列表默认每页 20 条，筛选后页码合理；
- 详情使用右侧抽屉，不出现不必要的整页跳转；
- 状态 tag 颜色与 queued/sending、completed/delivered、failed/rejected、pending/scheduled、uncertain/balance_blocked 语义一致；
- 时间以本地 +08:00 `YYYY-MM-DD HH:mm:ss` 显示；
- 手机号默认 mask，未授权角色没有解密入口；
- 390×844 下使用导航抽屉或卡片布局，无横向页面滚动，主要按钮触控区域至少 44 px；
- 键盘可操作，焦点可见，图标按钮有可读名称，Escape 可关闭移动导航。

## 7. 核心业务 UAT

> 下列用例与 [UAT.md](UAT.md) 编号一致。所有变更型用例先记录原值，完成后恢复；若恢复失败，本轮结果记为 FAIL 并停止后续相关用例。

### UAT-01 四角色登录与权限映射

- **优先级：** P0
- **测试角色：** admin01、approver01、operator01、viewer01
- **前置条件：** 四账号未锁定；使用四个独立隐私窗口。
- **测试数据：** 四个账号及统一 Mock 密码。
- **步骤：**

1. 分别登录四个账号并记录顶栏角色；
2. 按 6.1 对照菜单；
3. 对每个隐藏页面直接输入 URL；
4. viewer 尝试写操作，operator 尝试审批，approver 尝试发送，非 admin 尝试管理接口。

- **预期结果：** 正确账号登录成功；菜单与路由矩阵一致；越权请求返回 403 `FORBIDDEN` 或安全重定向；不存在通过 URL 绕过。当前仅证明 Mock 角色共享层，真实 AD 映射仍须另测。
- **证据：** 四角色首页、菜单截图和至少四个越权响应状态。
- **恢复：** 四个窗口全部退出并关闭。

### UAT-02 账号连续失败锁定

- **优先级：** P0
- **测试角色：** operator01；admin01 备用
- **前置条件：** 独立源 IP、无其他人使用 operator01；测试负责人批准锁定窗口。
- **测试数据：** operator01、错误密码。
- **步骤：**

1. 连续 5 次提交错误密码；
2. 第 6 次使用正确密码登录；
3. 记录错误码、时间和剩余锁定提示；
4. 等待 15 分钟后重试，或由授权运维按既定恢复流程清除测试锁定。

- **预期结果：** 第 6 次返回 HTTP 423、`ACCOUNT_LOCKED`；锁定期间正确密码也被拒；到期或受控恢复后可登录；错误不泄露 LDAP/Redis 细节。
- **证据：** 第 5 次失败、第 6 次锁定、恢复后登录截图。
- **恢复：** 确认 operator01 恢复可登录；不得通过重置全环境替代未记录的恢复。

### UAT-03 登录 IP 限流

- **优先级：** P0
- **测试角色：** 接口测试员、运维测试员
- **前置条件：** 使用不会影响团队的独立出口 IP；已确认解封方案。
- **测试数据：** 不存在或专用测试账号、错误密码。
- **步骤：**

1. 5 分钟内从同一独立 IP 发起 20 次失败登录；
2. 再用正确账号密码尝试登录；
3. 从另一未封禁 IP 验证服务仍正常；
4. 等待 15 分钟或执行批准的解封操作。

- **预期结果：** 达阈值后返回 HTTP 429、`RATE_LIMITED`；封禁 IP 的正确密码也被拒；其他 IP 不受影响；响应不泄露用户名是否存在。
- **证据：** 脱敏请求计数、429 响应、另一 IP 正常证据。
- **恢复：** 确认原 IP 可重新登录；共享 NAT 环境禁止执行本例。

### UAT-04 管理员强制下线

- **优先级：** P1
- **测试角色：** admin01、operator01
- **前置条件：** operator01 已登录并停留在批次列表。
- **测试数据：** operator01 活跃会话。
- **步骤：**

1. admin01 进入“用户与角色”；
2. 对 operator01 执行强制下线；
3. operator01 刷新页面或发起任一查询；
4. 查询审计日志。

- **预期结果：** operator01 下一请求返回 401 `UNAUTHORIZED` 并跳转登录；旧 token 不可继续用；审计包含执行人、目标用户和动作，但不包含 token。
- **证据：** 管理操作、operator 跳转、审计记录截图。
- **恢复：** operator01 重新登录，确认新会话正常。

### UAT-05 API 发送幂等

- **优先级：** P0
- **测试角色：** 接口测试员
- **前置条件：** 安全取得 app-oa API Key；Mock 状态正常。
- **测试数据：** `<测试手机号A>`、唯一 `biz_id=<RUN_ID>-idem`、notice 内容。
- **步骤：**

1. 使用相同请求体和 `biz_id` 调用发送接口；
2. 在 24 小时幂等期内立即重复同一请求；
3. 查询两次响应的 `batch_no`；
4. 由运维查看 Mock Send 安全计数，不导出号码原文。

- **预期结果：** 首次 200；第二次 200 且 `idempotent=true`、批次号相同；只产生一次厂商 Send；不重复扣配额。
- **证据：** 脱敏请求摘要、两次响应中的批次号/幂等字段、Send 计数。
- **恢复：** 无配置恢复；记录生成的批次号供后续清理。

### UAT-06 应用类别越权

- **优先级：** P0
- **测试角色：** 接口测试员
- **前置条件：** app-iam 仅允许 verify。
- **测试数据：** `<测试手机号A>`、market 内容、唯一 `biz_id`。
- **步骤：**

1. 使用 app-iam API Key 提交 category=market；
2. 查询是否生成批次；
3. 核对 Mock Send 计数。

- **预期结果：** HTTP 403、`CATEGORY_NOT_ALLOWED`；不创建可发送批次、不扣配额、不调用厂商。
- **证据：** 错误响应和无厂商调用的安全计数。
- **恢复：** 无。

### UAT-07 verify 号码级频控

- **优先级：** P1
- **测试角色：** 接口测试员
- **前置条件：** 记录当前 verify 分钟频控配置。
- **测试数据：** 同一 `<测试手机号A>`、两个不同 `biz_id`、两个 verify OTP。
- **步骤：**

1. 提交第一条 verify；
2. 1 分钟内向同号提交第二条；
3. 查询第二批次受理统计；
4. 检查 Redis 证据仅含 phone_hmac 键，不读取值中的敏感信息。

- **预期结果：** 第一条受理；第二条被频控剔除，`removed_freq_limit=1`，若全部剔除则为 422 `ALL_FILTERED`；Redis 键不含明文手机号。
- **证据：** 两次响应和脱敏 Redis 键模式。
- **恢复：** 等待分钟窗口自然结束；未修改配置。

### UAT-08 营销时间窗自动延期

- **优先级：** P1
- **测试角色：** operator01、admin01
- **前置条件：** 保存 `market_send_window` 原值；获准临时设置一个不包含当前时间的短窗口。
- **测试数据：** `<测试手机号A>`、market 内容、已勾选营销同意。
- **步骤：**

1. admin01 把营销窗口临时设为当前时刻之外；
2. operator01 提交 market；
3. 打开批次详情核对状态、计划时间和延期原因；
4. 到点前取消该批次。

- **预期结果：** 返回 200，状态 `scheduled`；计划时间为下一窗口起点，`deferred_reason=market_window`；取消成功。
- **证据：** 参数原值、批次详情、计划时间和取消结果。
- **恢复：** 在 finally 步骤恢复 `market_send_window` 原值并复读确认。

### UAT-09 营销退订语自动追加

- **优先级：** P1
- **测试角色：** operator01、运维测试员
- **前置条件：** `unsubscribe_auto_append=true`，记录退订语原值。
- **测试数据：** 不含退订语的 market 内容、`<测试手机号A>`。
- **步骤：**

1. 在人工发送页选择 market 并勾选同意；
2. 查看计费预估；
3. 提交后由运维检查 Mock 内存中的实际下发内容；
4. 对照批次计费条。

- **预期结果：** 下发内容尾部自动含配置的退订语；计费在追加之后计算；持久化和展示符合内容边界。
- **证据：** 提交前预估、批次计费和不含手机号的 Mock 内容摘要。
- **恢复：** 无；若临时改过退订语，恢复原值。

### UAT-10 Web 营销同意留痕

- **优先级：** P0
- **测试角色：** operator01、admin01
- **前置条件：** 人工发送页可用。
- **测试数据：** market 内容和 `<测试手机号A>`。
- **步骤：**

1. 不勾选营销同意并提交；
2. 勾选同意后再次提交；
3. admin01 查询相关审计日志。

- **预期结果：** 未勾选时 HTTP 422、`CONSENT_REQUIRED`；勾选后受理；审计记录 `consent_confirmed` 和操作人，只引用数量/批次，不含手机号列表。
- **证据：** 错误提示、成功批次号和审计截图。
- **恢复：** 无。

### UAT-11 审批阈值与本人回避

- **优先级：** P0
- **测试角色：** operator01、approver01、admin01
- **前置条件：** 审批阈值为 50；保存 operator01 原角色来源和覆盖状态。
- **测试数据：** 60 个批准测试号、market 内容、唯一 `<RUN_ID>`。
- **步骤：**

1. operator01 提交 60 号码 market；
2. 确认批次进入 `pending_approval`；
3. 由 admin 临时将 operator01 人工角色切为 approver；operator01 退出并重新登录，使新角色进入新的 JWT；
4. operator01 尝试审批本人批次；
5. admin 恢复 operator01 角色，operator01 再次退出并重新登录确认菜单恢复；
6. approver01 通过该批次，再次尝试重复审批。

- **预期结果：** 达阈值进入审批；本人审批返回 403 `SELF_APPROVAL_DENIED`；approver01 可通过；重复审批返回 409 `STATE_CONFLICT`；DB CHECK 与审计均生效。
- **证据：** 待审批、本人拒绝、他人通过、重复审批和审计记录。
- **恢复：** 恢复 operator01 的 AD/人工角色状态并重新登录验证菜单。

### UAT-12 审批过期与配额回补

- **优先级：** P1
- **测试角色：** admin01、operator01、运维测试员
- **前置条件：** 独立窗口；记录 `market_approval_threshold`、配额和任务状态基线；由授权 DBA 准备只更新本例审批行过期时间的参数化命令。
- **测试数据：** 一笔需审批 market 批次。
- **步骤：**

1. 如有需要，临时把 `market_approval_threshold` 设为 1；
2. 创建待审批批次并记录批次号和预扣配额；
3. 授权 DBA 仅将该批次对应 approval 行的 `expires_at` 调整为当前时间后 5 秒；不得把按小时计的全局 `approval_expire_hours` 改成非法小数；
4. 等待 6 秒并由运维触发审批过期扫描；
5. 查询批次、配额、alert_log 和审计。

- **预期结果：** 批次变为 `expired`；预扣配额完整回补且不重复；log-sink 写入 alert_log/日志，不请求外部渠道。
- **证据：** 参数原值、状态变化、回补前后计数和 alert_log 行。
- **恢复：** 无论成功失败都恢复 `market_approval_threshold`；确认没有其他审批行被修改，扫描任务与队列健康。

### UAT-13 计费条计算与配额一致性

- **优先级：** P0
- **测试角色：** operator01、接口测试员
- **前置条件：** 应用剩余配额充足。
- **测试数据：** 150 字最终内容、100 个批准测试号。
- **步骤：**

1. 在发送页查看计费预估；
2. 提交批次；
3. 查询 `est_segments`、`quota_cost` 和应用配额变化；
4. 对照统计报表计费条。

- **预期结果：** 单号 3 条，100 号码总成本 300；预估、预扣、批次、统计均调用同一口径且一致；签名/退订语计入最终长度。
- **证据：** 最终内容长度、预估、批次成本、配额前后与报表。
- **恢复：** 无；记录批次供统计对账。

### UAT-14 黑名单过滤与敏感词阻断

- **优先级：** P0
- **测试角色：** admin01、operator01
- **前置条件：** 准备两个未使用测试号和唯一敏感词 `<RUN_ID>-敏感`。
- **测试数据：** `<测试手机号A>` 加入黑名单，`<测试手机号B>` 正常。
- **步骤：**

1. admin01 将 A 加入黑名单；
2. 向 A+B 提交 notice；
3. 添加唯一敏感词并提交含该词的新批次；
4. 查询审计与厂商调用计数。

- **预期结果：** 第一批 `removed_blacklist=1` 且只受理 B；敏感词批次返回 422 `SENSITIVE_WORD`；被阻断内容不下发；审计无手机号列表。
- **证据：** 黑名单 mask、受理统计、敏感词错误和审计。
- **恢复：** 删除本例黑名单项和敏感词，确认列表恢复基线。

### UAT-15 定时批次取消与配额回补

- **优先级：** P1
- **测试角色：** operator01
- **前置条件：** 记录应用配额；计划时间设置为未来。
- **测试数据：** notice 内容、`<测试手机号A>`。
- **步骤：**

1. 创建未来定时批次；
2. 核对状态 `scheduled` 和预扣配额；
3. 到点前执行取消；
4. 重复取消一次并复查配额。

- **预期结果：** 首次取消后 `cancelled`，配额只回补一次；重复取消返回 409 `STATE_CONFLICT`，无二次回补；厂商未调用。
- **证据：** 状态、配额前后、冲突响应和无 Send 计数。
- **恢复：** 无。

### UAT-16 余额不足熔断与人工恢复

- **优先级：** P0
- **测试角色：** admin01、运维测试员
- **前置条件：** 独占故障窗口；记录 Mock、队列和余额基线。
- **测试数据：** Mock `next_send_code=999` 一次、一个 notice 批次。
- **步骤：**

1. 运维注入下一次 Send 返回 999；
2. 提交测试批次；
3. 查询批次、两个发送队列和告警；
4. 清除注入，确认余额恢复；
5. admin01 执行 `/queue/resume` 对应的人工恢复操作并观察续发。

- **预期结果：** 批次进入 `balance_blocked`，实时和批量队列均暂停；产生 crit alert_log 且不外呼；恢复后批次继续，已提交/uncertain chunk 不重投。
- **证据：** 注入前后 Mock 摘要、批次、队列、告警和恢复结果。
- **恢复：** 清除 Send 错误、恢复余额与队列；重新执行冒烟发送。

### UAT-17 Send 超时 uncertain 受控修复

- **优先级：** P0
- **测试角色：** 运维测试员、admin01
- **前置条件：** 独占故障窗口；记录 Mock latency 和 Send 计数。
- **测试数据：** `latency_ms=12000`、一个 notice 批次。
- **步骤：**

1. 运维设置 Mock 延迟 12 秒；
2. 提交批次，并等待发送 worker 对厂商调用触发 10 秒超时；业务受理 API 本身不应被描述为同步等待厂商；
3. 确认 chunk 为 `uncertain`，观察一段正常 worker 周期；
4. 核对没有第二次 Send；
5. 立即恢复 Mock 延迟为 0，触发或等待 reconcile；
6. 查询 raw customId 索引和最终状态。

- **预期结果：** 网络超时仅标记 uncertain，严禁自动重发；raw payload 加密存储；本例 Mock 已成功受理并生成成功报告，因此 reconcile 必须按 customId 修复为 `submitted`，总 Send 次数为 1。只有其他场景的 raw 证据明确证明失败时，通用状态机才允许 reconcile 修复为 failed。
- **证据：** 延迟基线、uncertain、Send 次数、reconcile 结果和无明文 raw 的检查。
- **恢复：** 必须先把 latency 恢复 0，再确认 uncertain 清单和队列回到基线。

### UAT-18 明确失败的一键重发

- **优先级：** P1
- **测试角色：** operator01
- **前置条件：** 准备 `<失败魔法号>`，Mock 无其他故障。
- **测试数据：** notice 内容、失败魔法号。
- **步骤：**

1. 提交批次并等待报告失败；
2. 在批次详情执行“失败重发”；
3. 查询新旧批次关系、审批/频控/配额处理；
4. 确认 uncertain 或 submitted 号码未被带入。

- **预期结果：** 只对明确 failed 号码创建新批次；`resend_of` 指向原批次；新批次重新走完整管控；原批次不被修改成伪成功。
- **证据：** 原批次失败、新批次号、`resend_of` 和接收数量。
- **恢复：** 无。

### UAT-19 模板申请、同步与变量校验

- **优先级：** P1
- **测试角色：** operator01、approver01、admin01
- **前置条件：** Mock 模板列表记录基线；准备唯一模板名。
- **测试数据：** 平台模板 `{1}{2}`，var_specs 最大长度 10 和 6。
- **步骤：**

1. 新建模板并提交厂商；
2. 运维检查 Mock BindTemplate 内容；
3. 同步状态至 approved；
4. 使用合法参数发送；
5. 使用超过 max_len 的参数再次发送。

- **预期结果：** 厂商格式为 `{s10}{s6}`；合法参数全量替换；超长返回 422 `TEMPLATE_PARAM_MISMATCH`，不调用 Send；模板状态正确。
- **证据：** 平台模板、Mock 转换结果、状态、成功与超长响应。
- **恢复：** 删除或标记本例模板，确保不影响后续选择列表。

### UAT-20 回调验签、重试、dead 与重推

- **优先级：** P0
- **测试角色：** admin01、运维测试员
- **前置条件：** 独占窗口；记录回调重试间隔、Mock callback 状态和回调计数。
- **测试数据：** 测试应用 callback 指向内部 Mock sink；Mock 前 5 次返回 500。
- **步骤：**

1. 临时把 5 次重试间隔设为 1 秒；
2. 设置 `callback_failures=5`、状态 500；
3. 产生一个可回调事件；
4. 等待任务进入 dead 并检查告警；
5. 核对每次请求的时间戳、raw body 和 HMAC 签名；
6. 测试超过 ±300 秒的签名请求被拒；
7. 恢复 Mock 为成功并手动重推。

- **预期结果：** 共 5 次重试后 dead 并告警；签名为时间戳与 raw body 的 HMAC-SHA256；过期请求拒绝；手动重推成功；callback_task 仅存引用与无 PII 元数据。
- **证据：** 次数、dead、alert_log、脱敏签名验证和重推结果。
- **恢复：** 恢复重试间隔、Mock callback 状态和保留计数，清理本例回调。

### UAT-21 号码查询、数据范围与解密审计

- **优先级：** P0
- **测试角色：** viewer01、admin01
- **前置条件：** `<测试手机号A>` 已有本部门消息；另有其他部门测试数据。
- **测试数据：** 测试手机号 A。
- **步骤：**

1. viewer01 精确搜索 A；
2. 检查列表和详情只显示 mask；
3. 尝试访问其他部门数据和解密接口；
4. admin01 搜索 A 并点击授权查看；
5. 查询审计。

- **预期结果：** 查询走 phone_hmac；viewer 只能看本部门 mask，越权返回 403；admin 授权解密成功且产生审计；审计不含手机号明文或密文列表。
- **证据：** viewer 结果、越权响应、admin 解密动作和审计引用。
- **恢复：** 关闭详情抽屉，不复制或保存解密结果。

### UAT-22 明文授权导出的密文落盘

- **优先级：** P0
- **测试角色：** approver01、运维测试员
- **前置条件：** 数据量不超过 10 万行；取得授权导出审批。
- **测试数据：** 限定时间、应用和部门的查询条件。
- **步骤：**

1. approver01 创建 `decrypted=true` 异步导出；
2. 等待完成并下载；
3. 在受控终端确认授权文件内容后立即安全删除；
4. 运维检查服务器落盘文件仍为 AES-GCM 密文，无法检索出 11 位号码；
5. 查询审计。

- **预期结果：** 未授权角色拒绝；授权任务异步完成；服务器文件加密，下载时流式解密；审计含 `decrypted=true`、条件与数量，不含号码列表。
- **证据：** 授权、任务状态、密文落盘检查和审计；不附明文导出文件。
- **恢复：** 安全删除下载文件；等待或触发导出文件生命周期清理。

### UAT-23 应用 API Key 双 Key 轮换

- **优先级：** P0
- **测试角色：** admin01、接口测试员
- **前置条件：** 使用专用测试应用或已批准轮换 app-oa；记录当前 Key 状态。
- **测试数据：** 旧 Key、新 Key、唯一 `biz_id`。
- **步骤：**

1. admin01 执行 Key rotate；
2. 在宽限期内分别用旧 Key 与新 Key提交安全请求；
3. 把宽限期临时缩短或等待到期；
4. 再用旧 Key 请求；
5. 查询审计，确认页面不回显完整 Key。

- **预期结果：** 宽限期内双 Key 可用；到期后旧 Key 返回 401 `UNAUTHORIZED`，新 Key 正常；Key 仅创建时一次性安全展示且不入日志/API 响应。
- **证据：** 不含 Key 的轮换状态、三次 HTTP 状态和审计。
- **恢复：** 确认新 Key 已安全托管；恢复宽限期配置；销毁临时 Key 副本。

### UAT-24 verify OTP 等长打码

- **优先级：** P0
- **测试角色：** operator01、运维测试员
- **前置条件：** `verify_otp_mask=true`；Mock 状态正常。
- **测试数据：** `<测试手机号A>`、包含随机 4–8 位 OTP 的 verify 内容。
- **步骤：**

1. 提交 verify 并记录 OTP 位数，不记录具体值；
2. 运维在 Mock 进程内受控核对实际下发内容使用原 OTP；
3. 查询批次详情、数据库安全断言、日志和回调展示；
4. 对比星号数量。

- **预期结果：** 计费与实际下发使用原文；持久化、日志、页面和回调边界均为等长星号；无 OTP 原文落盘；位数与原 OTP 相同。
- **证据：** 位数摘要、页面打码和“存储无原文”的计数断言，不保存原 OTP。
- **恢复：** 清除 Mock 内存测试记录或执行批准的状态清理。

### UAT-25 陌生报告 unmatched 处理

- **优先级：** P0
- **测试角色：** admin01、运维测试员
- **前置条件：** 记录 Mock report 队列和 unmatched 数量。
- **测试数据：** 陌生 taskId/customId、`<测试手机号A>`、成功报告状态。
- **步骤：**

1. 运维通过 Mock enqueue_report 注入陌生报告；
2. 触发或等待 report poll；
3. 在运维中心查询 unmatched；
4. 按号码 HMAC 搜索并创建密文导出；
5. 检查 raw 先加密落地和 processed 状态。

- **预期结果：** 报告不丢弃，落 unmatched_report 的 enc/hmac/mask 三列；raw payload AES-GCM 加密；可查询和密文导出；JSONB、日志和证据无明文手机号。
- **证据：** unmatched mask、计数、导出任务和存储结构断言。
- **恢复：** 清理本例 unmatched/Mock 队列或登记为测试数据，恢复基线计数。

### UAT-26 verify 发送量异常告警

- **优先级：** P1
- **测试角色：** admin01、运维测试员
- **前置条件：** 独占窗口；记录异常倍数、绝对量下限和统计基线。
- **测试数据：** app-iam 当日 verify 增量，同时满足大于基线 3 倍且不少于 500 的条件。
- **步骤：**

1. 通过批准脚本灌入统计测试数据；
2. 触发 anomaly 任务；
3. 查询 alert_log、仪表盘和日志；
4. 当日再次触发相同条件；
5. 构造只满足倍数、不满足绝对量的反例。

- **预期结果：** 双条件同时满足才告警；verify 告警为 crit，文案含“核查来源/停用 Key”等处置建议；同日去重；渠道为空时只落库和日志，不外呼。
- **证据：** 参数基线、crit 告警、去重和反例无告警。
- **恢复：** 恢复统计/阈值基线并清理测试告警，重跑任务确认正常。

### UAT-27 beat 任务心跳与手动触发

- **优先级：** P0
- **测试角色：** admin01、运维测试员
- **前置条件：** 独占维护窗口；记录所有 job 状态和最短预期间隔。
- **测试数据：** beat 服务、一个可安全手动触发的任务。
- **步骤：**

1. 记录目标任务最后一次成功时间和 `expect_interval_s`，再由运维停止 beat 容器；不得停止 API 内心跳巡检；
2. 等待“当前时间减最后成功时间”严格大于 `2 × expect_interval_s`；等于边界时尚不应判 stalled；
3. 越过边界后，再允许一个心跳巡检周期（默认 60 秒）和一次页面刷新缓冲，查询 job_stalled 告警和仪表盘任务格；从停止 beat 起的总等待上限设为 `2 × expect_interval_s + 90 秒`，超时才判失败；
4. 恢复 beat；
5. admin01 手动触发安全任务并查询审计。

- **预期结果：** API 内心跳巡检发现 stalled 并告警；恢复后任务格转绿；手动触发成功且审计；beat 单实例，第二实例抢锁失败退出。
- **证据：** 停止/恢复时间、告警、任务格和审计。
- **恢复：** 确认 beat 只有一个实例、全部关键任务恢复成功运行。

### UAT-28 单号码下行与回复时间线

- **优先级：** P1
- **测试角色：** operator01、viewer01、运维测试员
- **前置条件：** `<测试手机号A>` 未被频控或黑名单阻断。
- **测试数据：** 同号 notice、verify 和一条 Mock 上行回复。
- **步骤：**

1. 向 A 发送 notice；
2. 向 A 发送 verify；
3. 通过 Mock enqueue_reply 注入其回复；
4. 等待 reply poll；
5. 在号码搜索打开时间线并核对近 30 日徽标。

- **预期结果：** 三事件按 +08:00 时序合并；verify 内容已打码；回复右缩进并标“↩ 用户回复”；徽标数量正确；viewer 仍只见 mask 和本部门数据。
- **证据：** 脱敏时间线、事件顺序和徽标。
- **恢复：** 若回复内容为“TD”，移除自动加入的测试黑名单项；否则无。

## 8. 专项测试

### 8.1 API 契约与错误响应

接口测试统一从 `${TEST_BASE_URL}/api/v1` 进入，不直接访问回环端口。每类至少抽查：

| 场景 | HTTP/code | 关键断言 |
|---|---|---|
| 缺少/非法参数 | 400 `INVALID_PARAM` | `{code,message,detail}`，无框架堆栈 |
| 未认证/失效 Key | 401 `UNAUTHORIZED` | 不提示 Key 内容或是否接近正确 |
| 角色越权 | 403 `FORBIDDEN` | 无数据泄露 |
| 类别越权 | 403 `CATEGORY_NOT_ALLOWED` | 无批次、无扣费、无厂商调用 |
| 自审 | 403 `SELF_APPROVAL_DENIED` | DB 与接口双重阻断 |
| 不存在资源 | 404 `NOT_FOUND` | 不泄露其他租户/部门资源 |
| 非法状态流转 | 409 `STATE_CONFLICT` | 原状态不变 |
| 敏感词/模板/同意 | 422 对应业务 code | 不下发、不扣错配额 |
| 配额/限流 | 429 对应业务 code | 错误语义可区分 |
| 厂商明确错误 | 502 `VENDOR_ERROR` | 只含数值 code 与平台本地映射描述；不得出现厂商原始 msg |
| 余额阻断 | 503 `BALANCE_BLOCKED` | 双队列安全暂停 |

额外检查：Bearer JWT 只走 `Authorization` 请求头，不使用 cookie；新增/变更字段必须与 OpenAPI 一致；幂等命中为 200 而非错误。

### 8.2 厂商 Mock 故障矩阵

仅授权运维通过服务器回环地址执行。每次先 GET 保存安全基线，POST 注入，完成后清除并再次 GET 确认。不得把返回中的测试手机号或实际内容贴入证据。

| 注入 | 预期平台处理 |
|---|---|
| `next_send_code=429/5002/5003` | 1/2/4/8/16 秒指数退避，最多 5 次 |
| `next_send_code=1011` | 延迟 30 分钟重试 |
| `next_send_code=10010` | 延迟 5 分钟重试 |
| `next_send_code=1006` | vendor_batch_size 折半一次 |
| `next_send_code=999` | 余额熔断、双队列暂停、crit 告警 |
| `next_send_code=1000/1009/5000/10003/10004` | crit 告警并暂停双队列 |
| 参数类错误 | chunk failed，不自动重试 |
| `latency_ms=12000` | uncertain，禁止自动重发 |
| `requeue_reports=true` | 报告幂等，无重复回调/统计 |
| `enqueue_report` | unmatched 三列加密处理 |
| `enqueue_reply` | 回复入库、退订处理和时间线 |
| `callback_failures=5` | 5 次重试后 dead，手动重推 |

### 8.3 安全验收 SEC-01～SEC-07

在服务器上由授权运维从仓库根目录执行既有安全脚本；命令输出只归档 SEC 编号，不归档 token、号码或数据库载荷。

```bash
python3 scripts/security_acceptance.py \
  --base http://127.0.0.1:<API回环端口> \
  --compose-file deploy/docker-compose.yml \
  --secrets-dir deploy/secrets
```

必须验证：

- SEC-01：Mock 登录可取得有效 token，认证共享层正常；
- SEC-02：viewer 无管理权限；
- SEC-03：登录与筛选中的 SQL 元字符不能越过参数化和数据范围边界；
- SEC-04：loopback/private callback URL 未在白名单时于保存边界拒绝；出站前 DNS rebinding 双检由单元/集成测试另行覆盖；
- SEC-05：audit 载荷无手机号；七个运行角色的 audit_log 非 INSERT 权限在数据专项另行验证；
- SEC-06：verify OTP 持久化打码；
- SEC-07：全栈日志无手机号和已知凭据。

任何 SEC 失败均为验收阻断，不允许降低扫描范围或改脚本让其通过。

### 8.4 数据与隐私抽查

由 DBA/运维执行只返回计数或布尔值的安全查询：

- 七个职责角色均不是 owner/超级用户，且无 DDL 权限；
- 七角色对 audit_log 的 UPDATE/DELETE/TRUNCATE 被拒，metrics 连 INSERT 也被拒；
- raw_vendor_log 只有密文 payload 和无手机号 custom_ids；
- callback_task 无 phone/body 持久化；
- import_phone 使用 enc/hmac/mask/key_version；
- 剔除清单只有 phone_mask 和原因；
- decrypted 导出磁盘文件无法检索出符合手机号规则的连续数字；
- 全量日志在本轮时间窗内无符合手机号规则的连续数字；
- stat_daily 成功率为 `delivered/(delivered+failed)`，unknown/other 不入分母。

不得为了证据把密文、HMAC、完整日志或数据库行复制到测试报告。

### 8.5 性能冒烟

性能测试必须独占环境并得到测试负责人批准。禁止从公网无节制压测；推荐在服务器回环或同内网压测机执行现有脚本：

仓库完整 G2 会在 E2E 后复用已构建镜像并重建开发卷，再等待 ready、执行 seed-dev 后运行性能冒烟；手工独立运行下列脚本时，执行人仍须自行提供同等的干净、独占环境。

```bash
uv run --project backend python scripts/perf_smoke.py \
  --base http://127.0.0.1:<API回环端口> \
  --mock-base http://127.0.0.1:<MOCK回环端口> \
  --keys <受控临时Key文件> \
  --compose-file deploy/docker-compose.yml
```

通过标准：

- API 受理 30 RPS、持续 60 秒，P95 必须 `< 2000 ms`；
- verify 端到端 P95 必须 `< 2 秒`；
- bulk 与 verify 混合时实时 lane 不被 bulk 预留耗尽；
- 停止施压后 480 秒内排空；
- 阶段一 future scheduled 批次全部经正式取消接口回滚，`cancelled_scheduled_batches` 等于受理数，数据库无本轮 `pf-*` scheduled 遗留；
- 无重复发送、配额负数、uncertain 自动重投、worker 崩溃或敏感日志；
- 报告记录 commit、时间、请求数、P50/P95/P99、错误率、排空时间和资源峰值。

若现网存在其他测试，本例延期，不以共享环境结果作为基线。

### 8.6 兼容性与移动端

在 3.1 客户端矩阵执行以下检查：

- 登录卡片、侧栏、顶栏、筛选区、表格/卡片、分页、抽屉和弹窗无裁切；
- 390 px 下导航按钮可打开/关闭，点击菜单或 Escape 后收起；
- 表格密集页在移动端采用卡片/列表，不靠页面整体横向滚动；
- 输入法、日期选择、文件上传、确认弹窗和 toast 可用；
- 图表随容器调整，不遮挡图例或数值；
- 长应用名、长模板名、错误文案和空数据不破坏布局；
- 浏览器控制台无未处理异常、CSP violation 或外部字体/CDN 请求；
- prefers-reduced-motion 下不依赖动画表达关键状态。

每个客户端至少保留登录、仪表盘、人工发送/只读替代页、批次详情和一个管理页证据。

### 8.7 服务恢复与重启演练

仅在独立维护窗口由授权运维执行。顺序如下，每一步均先记录基线，完成后检查 Web、health、容器、任务和一次 Mock 冒烟发送：

1. `sudo systemctl restart sms-platform.service`；
2. `sudo systemctl restart docker.service`；
3. 按部署手册演练 `/run` 运行时 secrets 丢失后的自动重建；
4. 经批准执行整机 reboot 并重连；
5. 检查 systemd active、失败 unit 为 0、9 个测试容器状态、migrate、beat 单实例和公网 18080；
6. 只核对运行时 secret 文件名、owner 和 mode，不读取内容、长度或哈希。

失败时禁止 chmod 0644、让全部容器以 root 运行、把 secret 写入 `.env`、绕过包装器或同时启动第二个 beat。

### 8.8 自动化回归

远程黑盒自动 UAT 只在授权运维窗口执行，脚本默认覆盖 UAT-05～UAT-20、UAT-24～UAT-27 共 20 项，并负责 finally 恢复临时配置：

```bash
python3 scripts/e2e_api.py \
  --base http://127.0.0.1:<API回环端口> \
  --mock-base http://127.0.0.1:<MOCK回环端口> \
  --keys <受控临时Key文件> \
  --compose-file deploy/docker-compose.yml
```

仓库级完整 G2 应在隔离、全新 Mock 卷执行，而不是直接清空当前远程测试服务器：

```bash
API_PORT=18100 MOCK_VENDOR_PORT=19128 WEB_PORT=18180 bash scripts/verify_all.sh
```

G2 包含规格/硬规则、Ruff、Mypy、后端 pytest 与覆盖率、迁移双建库、OpenAPI、SEC、20 项自动 UAT、性能和 Node 24 前端检查。远程人工 UAT 与仓库 G2 二者均通过，才能形成完整测试结论。

## 9. 回归与验收标准

### 9.1 每轮结束清理

- [ ] 恢复所有临时 sys_config，并复读确认；
- [ ] Mock latency、错误码、余额、callback failures 和临时队列恢复基线；
- [ ] realtime/bulk 队列已恢复，uncertain 无本轮遗留；
- [ ] beat 仅一个实例且任务心跳正常；
- [ ] operator01 未锁定，IP 未封禁，人工角色覆盖已恢复；
- [ ] 删除本轮黑名单、敏感词、模板、测试回调和临时 Key；
- [ ] 明文授权导出已从测试终端安全删除；
- [ ] 不删除审计证据，不直接修改/删除 audit_log；
- [ ] 再执行 SMK-01～SMK-10 并记录结果。

### 9.2 通过标准

本轮可判定 PASS 需同时满足：

- P0、P1 用例 100% 通过；
- 28 项 UAT 全部有执行人、时间、结果和证据；
- Critical/Major 未关闭缺陷为 0；
- SEC-01～SEC-07 全部通过；
- 四角色和 18 页面矩阵通过，桌面与 390 px 无阻断问题；
- Mock 故障、配置、账号、队列和任务全部恢复；
- 日志、审计、导出和存储抽查无 PII/secret 泄露；
- 自动回归退出码 0，失败用例未被跳过或降级；
- 测试负责人、业务代表和技术负责人完成签署。

### 9.3 有条件通过与失败

- 有条件通过：仅存在已评估的 Minor/Trivial，具备责任人、计划版本和明确规避措施；
- 失败：任一 P0/P1 失败，或存在未关闭 Critical/Major，或环境恢复不完整；
- 阻塞：环境版本不明、外部依赖不可用、账号/Key 缺失或共享窗口冲突。阻塞不记作 PASS/FAIL，但必须登记持续时间和责任人。

### 9.4 最小回归集

紧急修复后至少重跑：SMK-01～SMK-10、UAT-01、UAT-04～UAT-07、UAT-10～UAT-11、UAT-13～UAT-18、UAT-21、UAT-24、受影响专项及对应相邻状态流转。涉及认证、安全、配额、发送 worker、reconcile、回调或加密的修改不得只跑单页面冒烟。

## 10. 测试报告模板

### 10.1 用例执行记录

| 字段 | 填写内容 |
|---|---|
| 轮次/用例 | 例如 R1 / UAT-17 |
| 执行人/角色 |  |
| 开始/结束时间（+08:00） |  |
| 环境 commit/镜像 |  |
| 前置条件 |  |
| 实际步骤差异 | 无；如有必须说明 |
| 实际结果 |  |
| 结果 | PASS / FAIL / BLOCKED |
| 证据编号 |  |
| 缺陷编号 |  |
| 恢复确认 | 已恢复 / 不适用 / 失败 |
| 复测人/时间 |  |

### 10.2 缺陷模板

```text
标题：[模块][等级] 简洁描述
环境：远程 Mock 测试环境 / commit / 镜像 digest
发现时间：YYYY-MM-DD HH:mm:ss +08:00
发现人：
关联用例：
前置条件：
复现步骤：
实际结果：
预期结果：
发生频率：必现 / 偶现（次数）
影响范围：角色 / 应用 / 类别 / 状态
安全影响：是否涉及 PII、凭据、越权、重复发送
证据编号：仅脱敏截图和安全日志摘要
临时规避：
恢复状态：
```

缺陷中禁止附 API Key、JWT、SSH 信息、secret 内容、明文手机号、完整 raw vendor payload 或明文导出文件。

### 10.3 测试日报

安全日报配置验收：先由授权运维一次性安装并启用 `security-report-collector.timer`，确认
`security-report-control/incoming/<上一上海自然日>.json` 由采集器生成；若证据源缺失，页面
必须显示 `unavailable`，不得用示例或零值伪造报告。管理员在 `/security-daily` 页面打开
“配置邮件”，确认可以保存启停状态、Resend API Key 和 1–3 个收件人；页面只显示 Key 是否
已配置。保存后启动独立 mailer，使用一条已生成日报执行“安全预览 → 手动投递”，确认状态
能够回写为已投递或投递失败。Key 不写入普通系统参数页，邮件正文仅来自脱敏结构化报告。

```text
日期：
轮次：
目标版本：
今日计划 / 完成：
用例：PASS __ / FAIL __ / BLOCKED __ / 未执行 __
新增缺陷：Critical __ / Major __ / Minor __ / Trivial __
已修复复测：
环境事件与恢复：
当前风险：
明日计划：
负责人：
```

### 10.4 最终测试报告

```text
# 企业短信管理平台 v1.6 测试报告

一、测试概况
- 测试周期：
- 环境与版本：
- 执行团队：
- 测试范围 / 未测范围：

二、结果摘要
- 环境冒烟：
- 角色与 18 页面：
- UAT 28 项：
- API/Mock/安全/性能/兼容性/恢复：
- 自动回归：

三、缺陷统计
- 按等级、模块、状态统计：
- Critical/Major 关闭证据：

四、安全与数据结论
- PII、凭据、审计不可变、OTP、导出、回调：

五、性能与稳定性结论
- 请求量、P95、错误率、排空时间、资源峰值：

六、遗留风险
- 风险、影响、规避、责任人、计划日期：

七、环境恢复确认
- 配置、Mock、队列、账号、任务、测试文件：

八、结论
- PASS / CONDITIONAL PASS / FAIL / BLOCKED：
- 理由：
```

### 10.5 签署表

| 责任方 | 姓名 | 结论 | 日期（+08:00） | 签名/审批记录 |
|---|---|---|---|---|
| 测试负责人 |  |  |  |  |
| 产品/业务代表 |  |  |  |  |
| 开发负责人 |  |  |  |  |
| 运维负责人 |  |  |  |  |
| 安全负责人 |  |  |  |  |

## 11. 开发测试临时 HTTPS 正式凭据入口

本节只验收传输与页面门禁，不安装或轮换正式 Key，不登记测试号码，不激活真实联调，不执行
管理员初始化，不调用厂商或发送短信。任何步骤都不得初始化数据库；必须保留 PostgreSQL
数据库、Docker volume、凭据 generation、测试号码和运行态数据。

### HTTPS-01：HTTP 入口失败关闭

1. 从普通 HTTP 入口登录并进入 `/configs`；
2. 打开“安装正式凭据”或“轮换正式凭据”；
3. 确认页面显示“打开正式凭据安全入口”说明；
4. 确认当前密码、SecretName、SecretKey 输入框均隐藏，主按钮 disabled；
5. 确认没有 step-up、seal session 或 credentials 网络请求。

预期：HTTP 入口不得降级，页面不显示浏览器原始英文异常。不要输入正式或占位 Key。

### HTTPS-02：一次性主机安装保持 inactive

cloudflared 固定版本为 `2026.7.2`，SHA-256 为
`ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd`。本地核验官方
binary 后上传；服务器不自行下载。源码由服务器目标 Git object 解包到固定 root-owned
`0700` staging，禁止执行普通上传源码或可变 checkout。按 `deploy/README.md` 的完整命令用
固定 `/usr/bin/python3` 执行安装器，并同时传入固定 `--cloudflared-file`、
`--source-root` 和 `--source-commit <40位目标SHA>`。安装器必须先完成全部 Git blob
逐字节验真和主机模块导入门禁，再安装 root-owned unit/runtime/manager/manifest/bootstrap，
创建 `/etc/sms-platform/test-host`，但不得创建真实激活 marker。执行
`systemd-analyze verify` 后运行：

```bash
sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH=/usr/local/libexec/sms-platform/test-secure-access \
  /usr/bin/python3 \
  /usr/local/libexec/sms-platform/test-secure-access/test_secure_access_manager.py status
```

预期：status 为 inactive，unit 未 enable、未启动，上传临时文件已清理。

### HTTPS-03：手机安全上下文

操作者发送“打开正式凭据安全入口”，由 Codex 执行：

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
```

打开唯一 `https://<label>.trycloudflare.com` URL 并重新登录。预期页面为
`isSecureContext=true`、`crypto.subtle` 可用，`/configs` 显示原有密封表单。Cloudflare
Quick Tunnel 仅限开发测试、无 SLA，最多 15 分钟。只确认表单出现；Codex 不读取、代填或
提交正式 Key。

### HTTPS-04：提前停止与自动过期

提前停止：

```bash
sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access stop
sudo /usr/bin/env SMS_PLATFORM_ROOT=/opt/sms-platform \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT=/run/sms-platform/secrets \
  SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials \
  /usr/local/sbin/sms-compose secure-access status
```

预期 status 为 inactive、旧 URL 不可用、无 cloudflared 进程、额外监听或 `/run` 状态。
另起一次入口不执行 stop，确认 15 分钟硬时限后同样自动关闭。两次验收后平台、控制 agent、
PostgreSQL、Redis、workers 和 beat 保持健康，数据库与 Docker volume 未变化。

### HTTPS-05：与快速更新隔离

代码默认只通过 `scripts/test_update.sh apply --ref origin/main` 按需更新；必须取得
`state=verified`。high-risk、迁移或控制面更新必须先取得目标 commit 的精确
`ci-gate=success`。
快速更新和一次性主机安装均不自动启动隧道、不安装或轮换正式 Key、不登记测试号码、不激活
真实联调、不执行管理员初始化、不初始化数据库。

## 附录 A：执行前后速查

### 执行前

1. 执行 `scripts/test_update.sh status`，确认 commit、`development-vendor-live`/Mock 档位及
   `controlled`/inactive 状态；不得在证据中记录真实连接值；
2. 执行环境冒烟；
3. 分配四账号、测试号池和证据目录；
4. 按档位保存配置、Mock 或受控真实联调状态、队列、任务和账号基线；
5. 高风险用例取得运维窗口批准。

### 执行后

1. 恢复所有临时状态；
2. 删除明文下载和临时 Key 文件；
3. 复跑环境冒烟；
4. 汇总 28 项 UAT、专项和自动回归；
5. 记录真实系统尚未覆盖的发布前事项；
6. 完成测试报告与签署。

## 附录 B：事实源索引

- [PRD.md](../PRD.md)：需求、状态机、NFR 与上线边界；
- [AGENTS.md](../AGENTS.md)：工程硬规则和 UI 约定；
- [openapi.yaml](../openapi.yaml)：接口契约；
- [vendor-api.md](vendor-api.md)：厂商与 Mock 精确契约；
- [UAT.md](UAT.md)：28 项权威验收编号；
- [TRACEABILITY.md](TRACEABILITY.md)：需求、实现、接口与 UAT 映射；
- [ACCEPTANCE.md](ACCEPTANCE.md)：自动门禁和安全验收索引；
- [LOCAL_TESTING.md](LOCAL_TESTING.md)：本地 Mock 账号与安全边界参考；
- [deploy/README.md](../deploy/README.md)：远程部署、端口和运维入口；
- [HANDOVER.md](../HANDOVER.md)：真实系统和发布前人工事项。
