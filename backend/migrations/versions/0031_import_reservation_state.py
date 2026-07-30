"""Web 导入包可恢复两阶段预留与唯一批次绑定。"""

from __future__ import annotations

from alembic import op

revision = "0031_import_reservation_state"
down_revision = "0030_vendor_event_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """expand/backfill：旧 used 包失败关闭，新 writer 只使用状态机列。"""

    op.execute(
        """
        ALTER TABLE import_task
          ADD COLUMN IF NOT EXISTS state VARCHAR(10) NOT NULL DEFAULT 'ready',
          ADD COLUMN IF NOT EXISTS reservation_id UUID,
          ADD COLUMN IF NOT EXISTS reserved_by_account_id BIGINT,
          ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS reservation_expires_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS consumed_batch_id BIGINT,
          ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS payload_purged_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        UPDATE import_task
        SET expires_at=LEAST(expires_at,now())
        WHERE used=true AND state='ready'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_import_task_state'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_task_state
              CHECK (state IN ('ready','reserved','consumed'));
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_import_task_reservation_state'
          ) THEN
            ALTER TABLE import_task
              ADD CONSTRAINT ck_import_task_reservation_state CHECK (
                (
                  state='ready'
                  AND reservation_id IS NULL
                  AND reserved_by_account_id IS NULL
                  AND reserved_at IS NULL
                  AND reservation_expires_at IS NULL
                  AND consumed_batch_id IS NULL
                  AND consumed_at IS NULL
                )
                OR (
                  state='reserved'
                  AND reservation_id IS NOT NULL
                  AND reserved_by_account_id IS NOT NULL
                  AND reserved_at IS NOT NULL
                  AND reservation_expires_at IS NOT NULL
                  AND consumed_batch_id IS NULL
                  AND consumed_at IS NULL
                )
                OR (
                  state='consumed'
                  AND reservation_id IS NOT NULL
                  AND reserved_by_account_id IS NOT NULL
                  AND reserved_at IS NOT NULL
                  AND reservation_expires_at IS NOT NULL
                  AND consumed_batch_id IS NOT NULL
                  AND consumed_at IS NOT NULL
                )
              );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_import_reserved_account'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT fk_import_reserved_account
              FOREIGN KEY(reserved_by_account_id)
              REFERENCES user_account(id) ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_import_consumed_batch'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT fk_import_consumed_batch
              FOREIGN KEY(consumed_batch_id)
              REFERENCES sms_batch(id) ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='uk_import_reservation_id'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT uk_import_reservation_id
              UNIQUE(reservation_id);
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='uk_import_consumed_batch'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT uk_import_consumed_batch
              UNIQUE(consumed_batch_id);
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_reservation_expiry
          ON import_task(state,reservation_expires_at)
          WHERE state='reserved'
        """
    )


def downgrade() -> None:
    """存在任何预留或已消费绑定时拒绝丢失恢复证据。"""

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM import_task
            WHERE state<>'ready'
               OR reservation_id IS NOT NULL
               OR consumed_batch_id IS NOT NULL
               OR payload_purged_at IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade import reservation state with active evidence';
          END IF;
        END $$;
        """
    )
    op.execute("DROP INDEX idx_import_reservation_expiry")
    op.execute(
        "ALTER TABLE import_task DROP CONSTRAINT ck_import_task_reservation_state"
    )
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_task_state")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT uk_import_consumed_batch")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT uk_import_reservation_id")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT fk_import_consumed_batch")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT fk_import_reserved_account")
    op.execute("ALTER TABLE import_task DROP COLUMN consumed_at")
    op.execute("ALTER TABLE import_task DROP COLUMN payload_purged_at")
    op.execute("ALTER TABLE import_task DROP COLUMN consumed_batch_id")
    op.execute("ALTER TABLE import_task DROP COLUMN reservation_expires_at")
    op.execute("ALTER TABLE import_task DROP COLUMN reserved_at")
    op.execute("ALTER TABLE import_task DROP COLUMN reserved_by_account_id")
    op.execute("ALTER TABLE import_task DROP COLUMN reservation_id")
    op.execute("ALTER TABLE import_task DROP COLUMN state")
