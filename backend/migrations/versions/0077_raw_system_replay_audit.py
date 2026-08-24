"""系统 raw 重放审计意图列与同一 lease epoch 唯一系统审计。"""

from __future__ import annotations

from alembic import op

revision = "0077_raw_system_replay_audit"
down_revision = "0076_raw_processing_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS system_replay_audit_state VARCHAR(16)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='raw_vendor_log_system_replay_audit_state_check'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT raw_vendor_log_system_replay_audit_state_check
              CHECK (system_replay_audit_state IN ('pending','completed'));
          END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_system_replay_audit_pending
          ON raw_vendor_log(id)
          WHERE processed = TRUE AND system_replay_audit_state = 'pending'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_raw_replay_system_epoch
          ON audit_log (object_id, ((after_val->>'lease_epoch')))
          WHERE action = 'raw_replay'
            AND actor_subject_kind = 'system'
            AND object_type = 'raw_vendor_log'
        """
    )
    op.execute(
        """
        GRANT SELECT (system_replay_audit_state)
          ON raw_vendor_log TO sms_metrics
        """
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (system_replay_audit_state) ON raw_vendor_log FROM sms_metrics"
    )
    op.execute("DROP INDEX IF EXISTS idx_audit_raw_replay_system_epoch")
    op.execute("DROP INDEX IF EXISTS idx_raw_system_replay_audit_pending")
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          DROP CONSTRAINT IF EXISTS raw_vendor_log_system_replay_audit_state_check
        """
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS system_replay_audit_state"
    )
