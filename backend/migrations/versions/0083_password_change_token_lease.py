"""首次改密令牌增加短租约与 fencing 状态。"""

from __future__ import annotations

from alembic import op

revision = "0083_password_change_token_lease"
down_revision = "0082_outbox_realtime_report_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE password_change_token
          ADD COLUMN IF NOT EXISTS processing_lease_id UUID,
          ADD COLUMN IF NOT EXISTS processing_lease_expires_at TIMESTAMPTZ
        """
    )
    op.execute(
        "ALTER TABLE password_change_token "
        "DROP CONSTRAINT IF EXISTS password_change_token_status_check"
    )
    op.execute(
        "ALTER TABLE password_change_token "
        "DROP CONSTRAINT IF EXISTS ck_password_change_processing_lease"
    )
    op.execute(
        """
        ALTER TABLE password_change_token
          ADD CONSTRAINT password_change_token_status_check CHECK (
            status IN ('available','processing','consumed','revoked','expired')
          )
        """
    )
    op.execute(
        """
        ALTER TABLE password_change_token
          ADD CONSTRAINT ck_password_change_processing_lease CHECK (
            (
              status='processing'
              AND processing_lease_id IS NOT NULL
              AND processing_lease_expires_at IS NOT NULL
              AND consumed_at IS NULL
            )
            OR (
              status<>'processing'
              AND processing_lease_id IS NULL
              AND processing_lease_expires_at IS NULL
            )
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_password_change_processing_lease
          ON password_change_token(processing_lease_expires_at)
          WHERE status='processing'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM password_change_token WHERE status='processing'
          ) THEN
            RAISE EXCEPTION
              'cannot remove password change leases while tokens are processing';
          END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_password_change_processing_lease")
    op.execute(
        """
        ALTER TABLE password_change_token
          DROP CONSTRAINT IF EXISTS ck_password_change_processing_lease,
          DROP CONSTRAINT IF EXISTS password_change_token_status_check,
          DROP COLUMN processing_lease_expires_at,
          DROP COLUMN processing_lease_id,
          ADD CONSTRAINT password_change_token_status_check CHECK (
            status IN ('available','consumed','revoked','expired')
          )
        """
    )
