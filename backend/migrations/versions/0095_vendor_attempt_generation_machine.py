"""vendor attempt 改为每个 generation 一行，invoking 后禁止自动切换。"""

from __future__ import annotations

from alembic import op

revision = "0095_vendor_attempt_generation_machine"
down_revision = "0094_send_inflight_reservation_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD COLUMN IF NOT EXISTS adapter_id VARCHAR(32) NOT NULL DEFAULT 'zhihui'
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS ck_sms_vendor_attempt_adapter_id
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD CONSTRAINT ck_sms_vendor_attempt_adapter_id
          CHECK (adapter_id ~ '^[a-z][a-z0-9_]{0,31}$')
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD COLUMN IF NOT EXISTS routing_reason VARCHAR(64)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD COLUMN IF NOT EXISTS invoke_started_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS sms_vendor_attempt_outcome_check
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD CONSTRAINT sms_vendor_attempt_outcome_check
          CHECK (outcome IN (
            'not_invoked','rejected','submitted','uncertain','failed',
            'retry_scheduled','delayed','paused','stale',
            'invoking','cancelled_before_invoke'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          RENAME CONSTRAINT sms_vendor_attempt_chunk_id_vendor_id_generation_key
          TO uk_sms_vendor_attempt_vendor_generation
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS uk_sms_vendor_attempt_generation
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD CONSTRAINT uk_sms_vendor_attempt_generation
          UNIQUE (chunk_id, generation)
        """
    )
    op.execute("DROP INDEX IF EXISTS uk_sms_vendor_attempt_irreversible")
    op.execute(
        """
        CREATE UNIQUE INDEX uk_sms_vendor_attempt_irreversible
          ON sms_vendor_attempt (chunk_id)
          WHERE outcome IN ('submitted','uncertain','invoking')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sms_vendor_attempt
        SET outcome='uncertain', updated_at=now()
        WHERE outcome='invoking'
        """
    )
    op.execute("DROP INDEX IF EXISTS uk_sms_vendor_attempt_irreversible")
    op.execute(
        """
        CREATE UNIQUE INDEX uk_sms_vendor_attempt_irreversible
          ON sms_vendor_attempt (chunk_id)
          WHERE outcome IN ('submitted','uncertain')
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS uk_sms_vendor_attempt_generation
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          RENAME CONSTRAINT uk_sms_vendor_attempt_vendor_generation
          TO sms_vendor_attempt_chunk_id_vendor_id_generation_key
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS sms_vendor_attempt_outcome_check
        """
    )
    op.execute(
        """
        UPDATE sms_vendor_attempt
        SET outcome='failed'
        WHERE outcome='cancelled_before_invoke'
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD CONSTRAINT sms_vendor_attempt_outcome_check
          CHECK (outcome IN (
            'not_invoked','rejected','submitted','uncertain','failed',
            'retry_scheduled','delayed','paused','stale'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS ck_sms_vendor_attempt_adapter_id
        """
    )
    op.execute("ALTER TABLE sms_vendor_attempt DROP COLUMN IF EXISTS updated_at")
    op.execute(
        "ALTER TABLE sms_vendor_attempt DROP COLUMN IF EXISTS invoke_started_at"
    )
    op.execute("ALTER TABLE sms_vendor_attempt DROP COLUMN IF EXISTS routing_reason")
    op.execute("ALTER TABLE sms_vendor_attempt DROP COLUMN IF EXISTS adapter_id")
