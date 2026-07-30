"""关联 HTTP、任务、Outbox、回调与事务审计，并收紧审计载荷。"""

from __future__ import annotations

from alembic import op

revision = "0033_correlation_audit_chain"
down_revision = "0032_async_import_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE outbox_event ADD COLUMN IF NOT EXISTS correlation_id UUID")
    op.execute("UPDATE outbox_event SET correlation_id=id")
    op.execute(
        """
        ALTER TABLE outbox_event
          ALTER COLUMN correlation_id SET NOT NULL,
          ALTER COLUMN correlation_id SET DEFAULT gen_random_uuid()
        """
    )
    op.execute("ALTER TABLE callback_task ADD COLUMN IF NOT EXISTS correlation_id UUID")
    op.execute("UPDATE callback_task SET correlation_id=event_id")
    op.execute(
        """
        ALTER TABLE callback_task
          ALTER COLUMN correlation_id SET NOT NULL,
          ALTER COLUMN correlation_id SET DEFAULT gen_random_uuid()
        """
    )
    op.execute(
        """
        ALTER TABLE audit_log
          ADD COLUMN IF NOT EXISTS correlation_id UUID
        """
    )
    op.execute("UPDATE audit_log SET correlation_id=gen_random_uuid() WHERE correlation_id IS NULL")
    op.execute(
        """
        ALTER TABLE audit_log
          ALTER COLUMN correlation_id SET NOT NULL,
          ALTER COLUMN correlation_id SET DEFAULT gen_random_uuid()
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_correlation "
        "ON outbox_event(correlation_id,created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_callback_correlation "
        "ON callback_task(correlation_id,created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_correlation "
        "ON audit_log(correlation_id,created_at DESC)"
    )
    op.execute(
        "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS ck_audit_payload_no_pii"
    )
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT ck_audit_payload_no_pii CHECK (
          (COALESCE((before_val - 'batch_no')::text,'') ||
           COALESCE((after_val - 'batch_no')::text,''))
            !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
          AND (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
            !~* '"(phones?|mobiles?|phone_enc|phone_hmac|phone_list|mobile_list|'
                 '[^"]*_(enc|hmac)|[^"]*token[^"]*|[^"]*secret[^"]*|'
                 '[^"]*password[^"]*|body|request|request_body|content|'
                 'ciphertext|encrypted(_list)?)"[[:space:]]*:'
        ) NOT VALID
        """
    )
    op.execute(
        """
        DO $audit_guard$
        BEGIN
          ALTER TABLE audit_log
            VALIDATE CONSTRAINT ck_audit_payload_no_pii;
        EXCEPTION
          WHEN check_violation THEN
            -- audit_log 不可更新/删除；保留历史行，同时约束所有新增载荷。
            NULL;
        END
        $audit_guard$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT ck_audit_payload_no_pii")
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT ck_audit_payload_no_pii CHECK (
          (COALESCE((before_val - 'batch_no')::text,'') ||
           COALESCE((after_val - 'batch_no')::text,''))
            !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
          AND (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
            !~ '"(phones|mobiles|phone_enc|phone_hmac|phone_list|mobile_list)"'
                '[[:space:]]*:'
        ) NOT VALID
        """
    )
    op.execute("DROP INDEX idx_audit_correlation")
    op.execute("DROP INDEX idx_callback_correlation")
    op.execute("DROP INDEX idx_outbox_correlation")
    op.execute("ALTER TABLE audit_log DROP COLUMN correlation_id")
    op.execute("ALTER TABLE callback_task DROP COLUMN correlation_id")
    op.execute("ALTER TABLE outbox_event DROP COLUMN correlation_id")
