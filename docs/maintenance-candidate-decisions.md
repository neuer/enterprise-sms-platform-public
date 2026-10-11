# F-11 / F-13 维护候选处理记录

对应 Issue #806。原报告基线为 `cf4ecfc09c4b4495562445177427214b738b3929`。
本次按生产构造、事务所有权、离线兼容和测试路径决定处理方式；没有把静态零引用当作
历史数据已经迁移的证据，也没有调整回调业务规则、发送状态机或数据库权限。

## 候选与处理依据

| 符号或入口 | 运行与离线路径 | 处理与兼容边界 | 对应回归 |
| --- | --- | --- | --- |
| `callback_worker` 的状态码常量、`classify_callback_http_status` | 实际投递由 `CallbackDelivery` 返回 `DeliveryOutcome`，`CallbackWorker` 决定重试；旧分类函数只有独立单元测试，既不参与任务注册，也不参与投递状态转换 | 删除旧分类函数与常量。成功仍是全部 2xx；其他状态均按 60/300/900/3600/3600 秒重试，五次后 dead 并告警 | `test_callback_worker` 组合真实 delivery/worker，覆盖 14 个状态与六个重试阶段；`test_callback_delivery` |
| `crypto` 的 legacy 方法及显式 migration 方法 | 历史 import/export 文件读取仍调用 legacy 解密；其他 legacy 包装方法服务于兼容格式和显式迁移。没有可证明全部历史数据已消失的清单 | 保留，无预设删除日期。默认绑定解密拒绝 legacy；显式 migration 读出后重新生成上下文绑定密文，不修改已有数据。下载入口仍遵循 #804 的格式、身份与授权检查 | `test_crypto` 历史 bytes/packed 格式、显式迁移、默认拒绝及错误上下文拒绝；现有 import/export 文件测试 |
| `PasswordAuthProvider` | 生产注册通过 `ProviderKindHandler` 与 local/AD handler 完成，调用参数包含记录及 purpose。旧 Protocol 没有承担这些注册合同；运维/CLI 没有按该名称动态注册 | 删除重复 Protocol；保留实际 Provider 选择、注册与失败禁止回退逻辑 | 认证后端、Provider 注册、密码认证与账号测试 |
| `SqlUserRepository.create_local_account` | Web 路由经 `UserService.create_local` 到 `SqlUserAdminRepository.create_local`；后者承接同事务授权、凭据、账号和审计。旧方法仅由其独立测试调用；空系统 `init-admin` 是不同的 CLI 合同 | 删除旧重复实现，断言迁移到实际创建仓储测试；不改动空系统引导、密码策略和初始化 TTY 边界 | `test_user_repository`、`test_user_service`、`test_auth_user_repository`，以及高风险管理员写入的真实 PostgreSQL/授权回归 |
| pipeline 的可选 `usage_ledger` 与 Redis-only 分支 | 三个生产构造点为 `api/messages.py`、`api/web_messages.py`、`services/vendor_test_uat.py`，均注入 `UsageLedgerService`；测试和运维路径中直接构造也逐一迁移 | 账本参数必填，显式 None 立即抛出 TypeError。删除流水线 FrequencyPort/QuotaPort 及直接频控、配额、退款回退；预留、批量频控、额度和释放请求只经账本。保留其他业务的兼容频控/配额服务，不由本项批量删除 | 流水线单元测试；真实 claim lease、ambiguous commit、uncertain resend、usage subject PostgreSQL 回归。存储提交不确定时只请求“未绑定预留释放”，不能直接回补已绑定事实 |
| 仓储 `await engine.dispose()` | 请求、worker、metrics 仓储的工厂经 `database_engine` 返回共享 `ManagedAsyncEngine`；应用私有 engine 工厂只在 CLI 和统一资源管理模块。脚本与测试还有独占 engine | 保留所有未改变所有权的 dispose 调用与兼容空操作，不将其替换为 close。共享池由 API lifespan、worker runtime、scheduler/dispatcher 的统一 shutdown 关闭；独占 CLI engine 的真实 dispose 保留 | 工厂边界测试、dispose/close 区分、异常 shutdown、prefork 丢弃；真实 PG 断连恢复与独立双子进程池验证 |
| 发送仓储 Mixin 的跨层依赖 | `SqlChunkStore` 组合现有 payload/attempt 等 Mixin，调用真实事务、载荷、预算与持久队列方法 | 新增事务、载荷、attempt 能力 Protocol；连接上下文返回 AsyncConnection。实际 SqlChunkStore 做结构兼容检查；无运行时空桩、无 MRO 改写。ManagedAsyncEngine 不再以 cast 伪装 AsyncEngine，也不以任意属性 Any 兜底 | `test_send_repository_type_contracts` 的正向实际组合与负向缺依赖/错误返回类型 fixture；发送事务、锁序、Outbox 和恢复回归 |

## 引用核查范围与保留决定

核查覆盖 `backend/app` 的导入/调用与构造点、任务及 Provider 注册、动态属性调用，
`backend/tests`、数据库迁移、`backend/scripts_support`、`scripts` 及相关文档。
本记录没有宣称上述目录不存在其他维护候选，也没有把保留的兼容接口称为已证实死代码。

`IdempotencyPort` 不再要求流水线未使用的 Redis 频控/配额键接口；
`IdempotencyCoordinator` 的历史键工具本身保留，避免把协议收紧扩展为未验收的兼容删除。
频控 HMAC 归并、幂等 claim 续租、用量预留复用及释放事件去重继续由既有实现负责。
notice 不执行号码频控；验证历史 HMAC 频控的测试改用实际执行频控的 verify 类别。

历史加密格式的退役需要独立、可核验的数据迁移依据。本次不执行数据重写，也不增加
默认解密降级。账号创建的集中化仅删除没有生产调用的旧仓储方法，不能绕过服务层密码
策略或事务内 `require_admin_intent` 授权。

## 连接池所有权与测试边界

`ManagedAsyncEngine.dispose()` 是兼容空操作；`close()` 才关闭底层池。
`database_engine` 现在返回这个真实类型，`connect`/`begin` 暴露准确的异步连接上下文，
因此错误属性访问和错误连接返回类型可在静态检查中拒绝。

真实进程探针先在父进程建立 PostgreSQL 连接，再 fork 两个子进程。各子进程调用生产的
`discard_inherited_runtime_resources`，并发获取连接、验证池上限和缓存复用，最后统一
关闭资源。两个子进程的数据库会话互不重叠，父进程原会话仍可用。这验证资源管理合同，
不等同于生产容量评估；性能测量属于 Issue #807。

验证证据必须绑定最终提交。定向用例通过不能代替提交前 `dev_check --changed`、精确
提交 CI、镜像门禁或合并后的复验；本记录不表示已部署到共享测试或生产环境。

### Linux prefork 资源生命周期补证

真实 Linux + PostgreSQL 探针显示，仅 `dispose(close=False)` 不足以保持父进程
会话：旧池失去引用后，asyncpg 连接析构可能通过继承的 socket 发送协议终止。
子进程因此将继承池保留到进程退出，仍执行 `dispose(close=False)` 并清空活动
缓存。继承池不会再借出连接，也不参与子进程 shutdown；其继承 FD 由进程退出
回收，不计为子进程创建的活动池。该集合只在 fork 初始化追加，不按请求增长。

回归探针在 fork 后强制 GC，要求两个子进程各自使用有界独立池、关闭自建
资源，且父进程原 PostgreSQL backend PID 保持不变。超时只终止探针自己的
独立进程组，并仅输出固定阶段标识，避免泄露数据库连接信息。
