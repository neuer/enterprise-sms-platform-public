"""为 stat_daily 增加 test_scope，并记录按日分层完成标记。

发布顺序：
1. 本迁移只增加维度、主键和完成标记。已有混合汇总变为 test_scope=all，
   不改写成 normal，也不在这条迁移里重算历史。
2. 先部署始终带显式 test_scope 条件的读取者（报表、仪表盘、异常检测）。
3. 确认旧读取者已经退出后，才运行会写入 all+test+normal 的应用并按日补算。
   本修订的应用代码就是这一版：写入三层，读取时只选一层。
4. 应用回退只能回到理解 test_scope 的版本。旧读取者若对多层求和会重复统计。

补算由 aggregate_stats 在现有按日锁和单事务内完成，每轮有界。
空日也写标记。筛选遇到未标记日期返回 REPORT_FILTER_NOT_READY。
迁移中断后整段事务回滚，再次 upgrade 可以前滚。
"""

from alembic import op

revision = "0130_stat_daily_test_scope"
down_revision = "0129_sms_accept_raw_replay"
branch_labels = None
depends_on = None

UPGRADE_STATEMENTS = (
    """
DO $stat_test_scope$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'stat_daily'
      AND column_name = 'test_scope'
  ) THEN
    ALTER TABLE stat_daily
      ADD COLUMN test_scope VARCHAR(8) NOT NULL DEFAULT 'all';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'stat_daily'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%test_scope%'
  ) THEN
    ALTER TABLE stat_daily
      ADD CONSTRAINT ck_stat_daily_test_scope
      CHECK (test_scope IN ('all','test','normal'));
  END IF;

  IF (
    SELECT string_agg(attribute.attname, ',' ORDER BY key.ord)
    FROM pg_constraint constraint_row
    JOIN unnest(constraint_row.conkey) WITH ORDINALITY AS key(attnum, ord) ON true
    JOIN pg_attribute attribute
      ON attribute.attrelid = constraint_row.conrelid
     AND attribute.attnum = key.attnum
    WHERE constraint_row.conrelid = 'stat_daily'::regclass
      AND constraint_row.contype = 'p'
  ) IS DISTINCT FROM 'stat_date,dim_type,dim_value,category,test_scope' THEN
    ALTER TABLE stat_daily DROP CONSTRAINT stat_daily_pkey;
    ALTER TABLE stat_daily
      ADD PRIMARY KEY (stat_date, dim_type, dim_value, category, test_scope);
  END IF;
END
$stat_test_scope$;
""",
    """
CREATE TABLE IF NOT EXISTS stat_scope_day (
    stat_date DATE NOT NULL PRIMARY KEY,
    aggregation_version INTEGER NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_stat_scope_day_version CHECK (aggregation_version >= 1)
)
""",
    "REVOKE ALL ON stat_scope_day FROM PUBLIC",
    "GRANT SELECT ON stat_scope_day TO sms_accept",
    "GRANT SELECT, INSERT, UPDATE ON stat_scope_day TO sms_send",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # 已有分层或完成标记时拒绝降级，避免旧读取者把多层快照加总。
    # 尚无分层数据时可以回到四列主键，供空库的迁移链回放继续向下。
    op.execute(
        """
        DO $stat_test_scope_down$
        BEGIN
          IF (
            to_regclass('stat_daily') IS NOT NULL
            AND EXISTS (SELECT 1 FROM stat_daily WHERE test_scope <> 'all')
          ) OR (
            to_regclass('stat_scope_day') IS NOT NULL
            AND EXISTS (SELECT 1 FROM stat_scope_day)
          ) THEN
            RAISE EXCEPTION
              'stat_daily test_scope rollback requires a reader that filters one scope';
          END IF;
        END
        $stat_test_scope_down$
        """
    )
    op.execute("DROP TABLE IF EXISTS stat_scope_day")
    op.execute("ALTER TABLE stat_daily DROP CONSTRAINT IF EXISTS ck_stat_daily_test_scope")
    op.execute("ALTER TABLE stat_daily DROP CONSTRAINT IF EXISTS stat_daily_pkey")
    op.execute("ALTER TABLE stat_daily ADD PRIMARY KEY (stat_date, dim_type, dim_value, category)")
    op.execute("ALTER TABLE stat_daily DROP COLUMN IF EXISTS test_scope")
