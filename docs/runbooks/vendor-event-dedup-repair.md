# 厂商事件乱序与历史重复修复

适用范围：`0030_vendor_event_facts` 升级后的报告投影核验，以及升级前已经写入的重复 `sms_reply` 业务投影。新事件由 `report_event.event_key` / `reply_event.event_key` 主键去重，不应再走人工修复。

## 1. 只读检测

先确认数据库已完成 0030，并使用仍保留全部历史版本的 AES/HMAC keyring：

```bash
cd backend
uv run python -m app.cli vendor-event-duplicate-audit
```

输出只包含 `group_key`、保留/重复事件键、mask 和数量，不包含手机号、密文或 HMAC。`group_count=0` 时停止，不执行后续 SQL。缺少任一历史 key version、任一密文无法认证或命令异常时必须失败关闭，禁止按 mask 猜测归并。

报告核验使用不可变事实和投影结果：

```sql
SELECT p.projection_changed, count(*)
FROM report_event_projection p
GROUP BY p.projection_changed
ORDER BY p.projection_changed;

SELECT m.id,m.created_at,m.status,m.report_status,m.report_time,
       trim(m.report_event_key) AS report_event_key
FROM sms_message m
WHERE m.report_event_key IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM report_event e
    WHERE e.event_key=m.report_event_key
  );
```

第二条查询必须返回 0 行。乱序但未赢得投影的 `report_event` 是审计事实，不是待删除垃圾。

## 2. 修复前条件

1. 取得可恢复的 PostgreSQL 快照并验证恢复点。
2. 暂停 reply 查询写入窗口；GetReply 原始密文不得删除。
3. 逐组复核审计命令输出；一次事务只处理一个 `group_key`。
4. 使用 `sms_owner` 执行。不得临时扩大 `sms_send` 对 event 表的 UPDATE/DELETE 权限。

## 3. 单组安全修复

把审计输出中的占位符替换为一组真实事件键。`expected_duplicate_count` 必须与输出完全相同：

```sql
BEGIN;
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;

CREATE TEMP TABLE repair_reply_projection(
  event_key char(64) PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO repair_reply_projection(event_key) VALUES
  ('<duplicate_event_key_1>'),
  ('<duplicate_event_key_2>');

SELECT r.id,r.created_at,trim(r.event_key) AS event_key,r.phone_mask
FROM sms_reply r
JOIN repair_reply_projection d ON d.event_key=r.event_key
ORDER BY r.created_at,r.id
FOR UPDATE;

DO $$
DECLARE actual_count integer;
BEGIN
  SELECT count(*) INTO actual_count
  FROM sms_reply r
  JOIN repair_reply_projection d ON d.event_key=r.event_key;
  IF actual_count <> <expected_duplicate_count> THEN
    RAISE EXCEPTION 'reply projection repair count mismatch: %', actual_count;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM sms_reply
    WHERE event_key='<keep_event_key>'::char(64)
  ) THEN
    RAISE EXCEPTION 'canonical reply projection is missing';
  END IF;
END $$;

WITH removed AS (
  DELETE FROM sms_reply r
  USING repair_reply_projection d
  WHERE r.event_key=d.event_key
  RETURNING r.event_key
)
INSERT INTO audit_log(
  actor,actor_subject_kind,action,object_type,object_id,after_val
)
SELECT
  'vendor-event-repair','system','reply_projection_deduplicate',
  'reply_event','<group_key>',
  jsonb_build_object(
    'keep_event_key','<keep_event_key>',
    'removed_projection_count',count(*)
  )
FROM removed;

COMMIT;
```

该事务只删除重复 `sms_reply` 投影。本修复不删除 `reply_event`、`raw_vendor_log` 或原始密文；`reply_event` 继续作为不可变审计事实，raw 数据仍按既定 90 天生命周期保留。任何断言失败都必须 `ROLLBACK`，不得改成无条件删除。

## 4. 修复后验证

重新运行只读审计命令，目标是 `group_count=0`。随后确认：

```sql
SELECT count(*) FROM reply_event;
SELECT count(*) FROM raw_vendor_log WHERE source='reply';
SELECT action,count(*)
FROM audit_log
WHERE action='reply_projection_deduplicate'
GROUP BY action;
```

修复前后 `reply_event` 与 reply raw 数量必须不变；只允许 `sms_reply` 数量按已审核的重复投影数减少。不要触碰 report/reply event 事实表，也不要通过重算批次统计来“修复”被正确忽略的乱序报告。
