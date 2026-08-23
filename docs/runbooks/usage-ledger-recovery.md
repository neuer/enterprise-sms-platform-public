# 配额与频控事实账本恢复手册

PostgreSQL 的 `usage_reservation`、配额明细、频控主体/alias/明细和 `usage_projection` 是唯一事实；Redis 只保存带版本的绝对值投影。发送入口遇到 Redis 不可确认、ready marker 缺失且正在重建时返回 HTTP 503 `USAGE_PROJECTION_UNAVAILABLE`。不得临时关闭此失败关闭边界，也不得手工把 Redis 计数设为零。

HMAC 轮换或并发若使同一号码出现多个 `usage_frequency_subject`，主体归并必须同时把
verify/market 窗口的 `usage_projection` 绝对值写入 canonical 键。只改 alias/entry 会留下
孤立投影并低估后续频控。过期 source 投影只有在仍被未终态 counted 明细引用时，才写成
expires_at 已过期的 canonical 墓碑供后续驳回/过期/取消释放命中；无引用则删除且不得
重建过期限流。生产轮换前确认该归并已上线，并盘点是否仍有一个号码对应多个未过期频控主体。

## 状态与自动恢复

- `reserved`：已建立受理事实，尚未与批次同事务提交。
- `committed`：批次已保存，额度和 accepted 号码频控继续占用。
- `release_requested`：终态或受理失败已在同一事务写入唯一 `usage.release` Outbox；PostgreSQL 绝对投影已扣减。
- `released`：Outbox 已把当前绝对投影覆盖到 Redis，重复消费不再扣减。
- `uncertain`：PostgreSQL 已写入事实，但 Redis 投影写入未确认；发送失败关闭，巡检会把超时预留转为释放请求。

`app.tasks.reconcile_usage_projection` 每 5 分钟恢复超过 10 分钟的 `reserved/uncertain` 预留，并比较 PostgreSQL 与 Redis 的聚合差异。指标为：

- `sms_usage_projection_drift_dimensions{kind="quota|frequency"}`；
- `sms_usage_projection_drift_absolute_delta{kind="quota|frequency"}`。

差异非零时产生 log-sink critical 告警。指标、告警和任务结果只包含聚合数量。

## 无 PII 解释

按批次或预留 UUID 解释计数来源：

```bash
sudo /usr/local/sbin/sms-compose exec -T api \
  python -m app.cli usage-ledger-explain --batch-no <32位批次号>

sudo /usr/local/sbin/sms-compose exec -T api \
  python -m app.cli usage-ledger-explain --reservation-id <UUID>
```

输出只包含应用/部门、配额维度、不可逆主体 UUID、窗口、是否计数和释放状态；不得扩展为手机号、密文、HMAC、摘要或密钥版本的回显。

## Redis flush 或漂移后的安全重建

1. 记录当前两组漂移指标、Outbox backlog/dead 数量和 API 503 比例，不读取或导出号码维度。
2. 确认 PostgreSQL 正常且 0028 迁移已到 head；不要清空事实表、改写状态或删除 Outbox。
3. 执行版本化绝对覆盖：

   ```bash
   sudo /usr/local/sbin/sms-compose exec -T api \
     python -m app.cli usage-projection-rebuild
   ```

   命令可与受理并发；Lua 版本比较会拒绝旧快照覆盖更新值。重建审计只记录配额、频控和总维度数量。
4. 等待下一次巡检，确认两组漂移指标归零、critical 告警不再新增，发送入口不再返回 `USAGE_PROJECTION_UNAVAILABLE`。
5. 若仍有差异，保留失败关闭，检查 Outbox dead-letter 与 `release_requested/uncertain` 聚合数量；不得用 Redis `SET/DEL/FLUSH*` 直接修数。

## 保留与降级

`usage_ledger_retention_days` 默认 90 天。housekeeping 仅删除超过保留期、窗口已过期且状态为 `released/committed` 的事实，并清理无引用频控主体；活动预留和释放中的事实不会删除。降级 0028 前必须没有未释放事实和 `usage.release` 事件，否则迁移拒绝执行。
