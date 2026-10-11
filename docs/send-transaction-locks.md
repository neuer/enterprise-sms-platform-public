# 发送结果持久化与恢复事务锁序

供应商 HTTP 调用和数据库结果事务分开。`SendWorker` 不因结果落库失败重新发送，
也不在 `submitted`、`uncertain` 或提交结果无法证明时自动切换供应商。

## 行锁顺序

单分片涉及相同行时遵循 `sms_chunk → sms_batch → sms_vendor_attempt → balance`。
`lock_send_context` 在分别取得分片、批次锁后读取当前状态，再按 generation/id 锁定尝试历史。
结果持久化同时核对分片、尝试、generation 和调用方提供的 batch 归属。
CAS 失败回滚整个结果事务，不提交单独更新的 attempt。

跨分片事务必须先确定完整候选集合。`lock_send_chunks` 按主键依次预锁全部 chunk、
batch、attempt，然后按 app ID 预锁候选预留关联的 balance，之后才能逐条处理。
不能在处理第一个分片取得 batch 或 balance 后，继续等待第二个未预锁的分片。
同一批次的拆分先取得 advisory 锁；批量拆分先按 batch ID 取得全部 advisory 锁，
再进入上述行锁顺序。新子片及其 Outbox 仍在同一事务创建。

恢复器将 legacy safe reject、停滞结果、容量拆分、预留补绑、误释放恢复、
余额回收与投递登记分为独立短事务。每个修改阶段重新核验当前状态，
阶段中途失败不依赖前一阶段回滚；重复运行依据 CAS、唯一约束与账本状态收敛。
仅修改 batch 和余额、不访问 chunk 的预留补绑/误释放阶段，先预锁全部 batch，
再按 app ID 锁全部 balance。余额回收阶段先冻结各子阶段候选，按 app ID 预锁
整个事务的余额集合，再执行回收和守恒检查，不在后续查询中扩大候选集合。

审批取消等仅锁 batch、消息或账本的路径不应添加 chunk 锁。
`prepare_chunks` 持有 batch 后只创建当前事务的新分片；新分片提交前不对其他 worker 可见。
新增路径必须检查实际锁集合，不能只看函数中的第一条 `FOR UPDATE`。

## 有限重试与未知提交

`finalize_vendor_attempt` 仅重试数据库明确报告 `40P01`、且原事务上下文已经退出的失败，
最多执行三次相同结果事务，间隔为 10–60ms 的有界抖动。
数据库循环预算为 28 秒，最终故障观测最多 2 秒，与恢复器使用的 30 秒持久化预算一致。
重试范围不包含厂商 HTTP，也不包含提交后的任务投递。

CAS 丢失、锁等待超时、连接错误、未知错误分别报告
`cas_lost/db_timeout/db_connection/unexpected`；死锁报告 `db_deadlock`。
提交确认丢失时不猜测回滚，不重试结果事务，改读权威 attempt/chunk 状态。
无法证明结果时保留隔离状态，等待恢复或人工核验；禁止直接重发。

## 可观测性和验收

日志只写固定 phase/reason、内部 chunk/attempt/generation，不写异常原文、SQL、参数或厂商响应。
`send_db_failure_event` 保存固定类别，用于跨 worker 的 `sms_send_db_failures` 聚合。
终止重试或其他失败产生去重告警。数据库完全不可用时，计数或告警也可能无法保存；
`send_db_observation_unavailable` 明确标记该缺口，计数不能作为“从未失败”的证明。

`tests/integration/test_send_lock_order_postgres.py` 使用真实仓储、独立 PostgreSQL 连接、
显式事件和服务器 `pg_locks` 检查验证 100 轮锁等待、同批次多分片、跨 app 反向候选顺序、
事务中止后的数据库重试，以及提交前回滚/提交后确认丢失。错误注入发生在结果事务内部，
发送网关使用计数替身；测试不调用真实厂商。另行运行 split、inflight、reconcile 和
failover 集成回归，不能以这一个文件替代完整门禁或部署验收。
