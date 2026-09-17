"""持久化 UAT 受理关联和自动日报非敏感发信配置投影。"""

from alembic import op

revision = "0117_review_acceptance_facts"
down_revision = "0116_usage_release_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT (effect_applied_at) ON sms_uncertain_resolution TO sms_metrics")
    op.execute(
        "GRANT SELECT (event_type,last_error,next_attempt_at) ON outbox_event TO sms_metrics"
    )
    op.execute("""
ALTER TABLE vendor_test_operation
  ADD COLUMN IF NOT EXISTS acceptance_reference_required BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS acceptance_biz_id VARCHAR(32),
  ADD COLUMN IF NOT EXISTS acceptance_app_id BIGINT REFERENCES app(id) ON DELETE RESTRICT;
    """)
    op.execute(
        "ALTER TABLE vendor_test_operation ALTER COLUMN acceptance_reference_required SET "
        "DEFAULT true"
    )
    op.execute("""
DO $$ BEGIN
IF NOT EXISTS (
  SELECT 1 FROM pg_constraint WHERE conrelid='vendor_test_operation'::regclass
    AND conname='ck_vendor_test_acceptance_reference'
) THEN
ALTER TABLE vendor_test_operation
  ADD CONSTRAINT ck_vendor_test_acceptance_reference CHECK (
    (acceptance_biz_id IS NULL AND acceptance_app_id IS NULL)
    OR (operation_type='uat_send' AND acceptance_biz_id IS NOT NULL
        AND length(acceptance_biz_id)>0 AND acceptance_app_id IS NOT NULL)
  );
END IF; END $$;
    """)
    op.execute("""
INSERT INTO sys_config(key,value,value_type,description)
SELECT 'security_daily_recipient_set_digest',
       encode(digest(convert_to(COALESCE(
         string_agg(lower(btrim(address)),',' ORDER BY lower(btrim(address))),''),
         'UTF8'),'sha256'),'hex'),
       'str','安全日报收件集合不可逆摘要'
FROM security_daily_recipient
ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
IF EXISTS (
  SELECT 1 FROM vendor_test_operation
  WHERE acceptance_reference_required OR acceptance_biz_id IS NOT NULL
    OR acceptance_app_id IS NOT NULL
) THEN
  RAISE EXCEPTION 'acceptance evidence cannot be downgraded';
END IF;
END $$;
    """)
    op.execute("""
ALTER TABLE vendor_test_operation
  DROP CONSTRAINT IF EXISTS ck_vendor_test_acceptance_reference,
  DROP COLUMN IF EXISTS acceptance_reference_required,
  DROP COLUMN IF EXISTS acceptance_biz_id,
  DROP COLUMN IF EXISTS acceptance_app_id;
    """)
    op.execute("DELETE FROM sys_config WHERE key='security_daily_recipient_set_digest'")
    op.execute("REVOKE SELECT (effect_applied_at) ON sms_uncertain_resolution FROM sms_metrics")
    op.execute(
        "REVOKE SELECT (event_type,last_error,next_attempt_at) ON outbox_event FROM sms_metrics"
    )
