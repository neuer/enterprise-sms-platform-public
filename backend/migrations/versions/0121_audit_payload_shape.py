"""审计载荷与批次引用类型收紧；历史审计只验证，不改写或删除。"""

from alembic import op

revision = "0121_audit_payload_shape"
down_revision = "0120_uat_recovery_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 既存 batch_no:null 是旧导出的合法缺省，保留原证据；触发器阻断新写入。
    # 其他不合规历史载荷仍失败关闭，不改写审计。
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS ck_audit_payload_shape")
    op.execute("""
        ALTER TABLE audit_log ADD CONSTRAINT ck_audit_payload_shape CHECK (
      CASE WHEN before_val IS NULL THEN TRUE
        WHEN jsonb_typeof(before_val) <> 'object' THEN FALSE
        WHEN NOT (before_val ? 'batch_no') THEN TRUE
        WHEN before_val->'batch_no' = 'null'::jsonb THEN TRUE
        ELSE COALESCE(jsonb_typeof(before_val->'batch_no') = 'string'
          AND (before_val->>'batch_no') ~ '^[0-9a-f]{32}$', FALSE) END
      AND
      CASE WHEN after_val IS NULL THEN TRUE
        WHEN jsonb_typeof(after_val) <> 'object' THEN FALSE
        WHEN NOT (after_val ? 'batch_no') THEN TRUE
        WHEN after_val->'batch_no' = 'null'::jsonb THEN TRUE
        ELSE COALESCE(jsonb_typeof(after_val->'batch_no') = 'string'
          AND (after_val->>'batch_no') ~ '^[0-9a-f]{32}$', FALSE) END
    )
    """)

    op.execute("""
CREATE OR REPLACE FUNCTION enforce_new_audit_payload_shape()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.before_val ? 'batch_no' AND NEW.before_val->'batch_no' = 'null'::jsonb)
     OR (NEW.after_val ? 'batch_no' AND NEW.after_val->'batch_no' = 'null'::jsonb) THEN
    RAISE EXCEPTION 'invalid audit batch reference'
      USING ERRCODE = '23514', CONSTRAINT = 'ck_audit_payload_shape';
  END IF;
  RETURN NEW;
END;
$$;
    """)

    op.execute("REVOKE ALL ON FUNCTION enforce_new_audit_payload_shape() FROM PUBLIC")

    op.execute("DROP TRIGGER IF EXISTS trg_audit_payload_shape ON audit_log")

    op.execute("""
CREATE TRIGGER trg_audit_payload_shape
BEFORE INSERT OR UPDATE ON audit_log
FOR EACH ROW EXECUTE FUNCTION enforce_new_audit_payload_shape();
    """)


def downgrade() -> None:
    # 应用回退不得撤销载荷保护，也不改写历史审计。仅回退 revision 标记；
    # 旧 writer 若仍写显式空批次引用，会安全拒绝，须先采用省略字段的兼容写法。
    # 更早迁移的专属回退保护仍由各自 downgrade 执行。
    pass
