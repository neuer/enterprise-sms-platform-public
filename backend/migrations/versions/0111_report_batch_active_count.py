"""批次活跃消息计数惰性初始化；迁移不扫描、回填历史消息。"""

from __future__ import annotations

from alembic import op

revision = "0111_report_batch_active_count"
down_revision = "0110_uncertain_child_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_batch
            ADD COLUMN IF NOT EXISTS active_message_count INTEGER,
            ADD COLUMN IF NOT EXISTS active_message_count_token UUID
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION invalidate_legacy_batch_active_count()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        BEGIN
          IF NEW.active_message_count_token IS NOT DISTINCT FROM OLD.active_message_count_token THEN
            NEW.active_message_count := NULL;
            NEW.active_message_count_token := NULL;
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION invalidate_legacy_batch_active_count() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_batch_invalidate_legacy_active_count ON sms_batch")
    op.execute(
        """
        CREATE TRIGGER trg_batch_invalidate_legacy_active_count
        BEFORE UPDATE OF delivered,failed,unknown_cnt ON sms_batch
        FOR EACH ROW EXECUTE FUNCTION invalidate_legacy_batch_active_count()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_batch_invalidate_legacy_active_count ON sms_batch")
    op.execute("DROP FUNCTION IF EXISTS invalidate_legacy_batch_active_count()")
    op.execute(
        "ALTER TABLE sms_batch DROP COLUMN IF EXISTS active_message_count, "
        "DROP COLUMN IF EXISTS active_message_count_token"
    )
