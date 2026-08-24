"""raw_vendor_log 处理租约 fencing 与 processed 交叉一致性。"""

from __future__ import annotations

from alembic import op

revision = "0076_raw_processing_lease"
down_revision = "0075_raw_parse_eligibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线建库已含租约列与交叉 CHECK；存量升级在此补齐。
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS processing_lease_id UUID
        """
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS processing_lease_epoch BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS processing_lease_expires_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='raw_vendor_log_processing_lease_epoch_check'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT raw_vendor_log_processing_lease_epoch_check
              CHECK (processing_lease_epoch >= 0);
          END IF;
        END $$
        """
    )
    op.execute(
        """
        UPDATE raw_vendor_log
        SET parse_state='unattempted',
            replay_eligibility=CASE
              WHEN replay_eligibility='never' THEN 'never'
              ELSE replay_eligibility
            END
        WHERE processed=false AND parse_state='processed'
        """
    )
    op.execute(
        """
        UPDATE raw_vendor_log
        SET parse_state='processed',
            replay_eligibility='never'
        WHERE processed=true
          AND (parse_state<>'processed' OR replay_eligibility<>'never')
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_raw_vendor_processed_consistency'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT ck_raw_vendor_processed_consistency
              CHECK (
                (processed = FALSE AND parse_state <> 'processed')
                OR (
                  processed = TRUE
                  AND parse_state = 'processed'
                  AND replay_eligibility = 'never'
                )
              );
          END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_processing_lease_epoch
          ON raw_vendor_log(processing_lease_expires_at, id)
          WHERE processed=FALSE AND processing_lease_id IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE worker_lease_event
          DROP CONSTRAINT IF EXISTS worker_lease_event_task_kind_check
        """
    )
    op.execute(
        """
        ALTER TABLE worker_lease_event
          ADD CONSTRAINT worker_lease_event_task_kind_check
          CHECK (task_kind IN ('callback','export','raw'))
        """
    )
    op.execute(
        """
        GRANT UPDATE (
          parse_state, replay_eligibility, error, processed, replay_attempts,
          processing_started_at, processing_lease_id, processing_lease_epoch,
          processing_lease_expires_at
        ) ON raw_vendor_log TO sms_accept
        """
    )
    op.execute("GRANT INSERT ON worker_lease_event TO sms_accept")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE worker_lease_event_id_seq TO sms_accept"
    )
    op.execute(
        """
        GRANT SELECT (
          processing_lease_epoch, processing_lease_expires_at
        ) ON raw_vendor_log TO sms_metrics
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT (
          processing_lease_epoch, processing_lease_expires_at
        ) ON raw_vendor_log FROM sms_metrics
        """
    )
    op.execute(
        "REVOKE USAGE, SELECT ON SEQUENCE worker_lease_event_id_seq FROM sms_accept"
    )
    op.execute("REVOKE INSERT ON worker_lease_event FROM sms_accept")
    op.execute(
        """
        REVOKE UPDATE (
          parse_state, replay_eligibility, error, processed, replay_attempts,
          processing_started_at, processing_lease_id, processing_lease_epoch,
          processing_lease_expires_at
        ) ON raw_vendor_log FROM sms_accept
        """
    )
    op.execute(
        """
        ALTER TABLE worker_lease_event
          DROP CONSTRAINT IF EXISTS worker_lease_event_task_kind_check
        """
    )
    op.execute(
        """
        ALTER TABLE worker_lease_event
          ADD CONSTRAINT worker_lease_event_task_kind_check
          CHECK (task_kind IN ('callback','export'))
        """
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_processed_consistency"
    )
    op.execute("DROP INDEX IF EXISTS idx_raw_processing_lease_epoch")
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          DROP CONSTRAINT IF EXISTS raw_vendor_log_processing_lease_epoch_check
        """
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS processing_lease_expires_at"
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS processing_lease_epoch"
    )
    op.execute("ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS processing_lease_id")
