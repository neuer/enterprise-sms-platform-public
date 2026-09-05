# Web unknown 人工重发（system_effect）

Web 批次进入 `completed_unknown` / `unknown_terminal` 后，只能经双人处置创建
child，不得把旧分片改回 pending，也不得绕过 Usage Ledger。

## 主体

- API 来源：真实 source app + 源批次 `dept`。
- Web 来源：受控内部 app `system-uncertain-resend`（正数 ID、日配额 10000、
  `usage_subject_kind=system_effect`），部门额度仍用源批次 `dept`。
- 禁止 `app_id=-1`、禁止把固定字符串 `web` 当作部门。
- system app 不出现在应用管理列表，也不能用 API Key 外呼。

## 值班

1. 管理员 A 提案 `resend_new_batch`，管理员 B 确认。确认时固化 `source_dept`。
2. Outbox worker 锁定 resolution 后构造 `UncertainResendContext`；HTTP 不能传入
   usage subject。
3. child 的稳定 biz_id 为 `manual-resend:<resolution>:<generation>`。Pipeline
   成功但 child 映射前崩溃时，按该键找回，不双扣额度。
4. 源应用停用、类别收回、部门缺失、双人账号失效：转入
   `manual_intervention_required`，不要无限重试。
5. PostgreSQL / Redis / 锁超时：`retryable_effect_error`，可重投 Outbox。

## 指标

不新增 Prometheus 族（D020/D108）。值班看 `sms_uncertain_resolution.state`
与 `sms_uncertain_child.recovered` 的低基数列；禁止账号、resolution、batch_no、
手机号标签。
