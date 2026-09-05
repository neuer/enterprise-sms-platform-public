"""幂等 Claim 增加权威状态机与完成态，heartbeat 续 PostgreSQL 租约。"""

from __future__ import annotations

from alembic import op

revision = "0099_idempotency_claim_lease_lifecycle"
down_revision = "0098_vendor_attempt_atomic_finalize"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE idempotency_claim
          ADD COLUMN IF NOT EXISTS state VARCHAR(16) NOT NULL DEFAULT 'active',
          ADD COLUMN IF NOT EXISTS batch_id BIGINT REFERENCES sms_batch(id),
          ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS release_reason VARCHAR(32)
        """
    )
    op.execute(
        """
        ALTER TABLE idempotency_claim
          DROP CONSTRAINT IF EXISTS ck_idempotency_claim_state
        """
    )
    op.execute(
        """
        ALTER TABLE idempotency_claim
          ADD CONSTRAINT ck_idempotency_claim_state
          CHECK (state IN ('active','completed','released','expired'))
        """
    )
    op.execute(
        """
        ALTER TABLE idempotency_claim
          DROP CONSTRAINT IF EXISTS ck_idempotency_claim_state_shape
        """
    )
    op.execute(
        """
        ALTER TABLE idempotency_claim
          ADD CONSTRAINT ck_idempotency_claim_state_shape CHECK (
            (
              state='active'
              AND batch_id IS NULL
              AND completed_at IS NULL
              AND released_at IS NULL
              AND release_reason IS NULL
            )
            OR (
              state='completed'
              AND batch_id IS NOT NULL
              AND completed_at IS NOT NULL
              AND released_at IS NULL
            )
            OR (
              state='released'
              AND batch_id IS NULL
              AND released_at IS NOT NULL
              AND release_reason IS NOT NULL
            )
            OR (
              state='expired'
              AND batch_id IS NULL
            )
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_idempotency_claim_active
          ON idempotency_claim (expires_at)
          WHERE state='active'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_idempotency_claim_active")
    op.execute(
        "ALTER TABLE idempotency_claim DROP CONSTRAINT IF EXISTS ck_idempotency_claim_state_shape"
    )
    op.execute(
        "ALTER TABLE idempotency_claim DROP CONSTRAINT IF EXISTS ck_idempotency_claim_state"
    )
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS release_reason")
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS released_at")
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS completed_at")
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS batch_id")
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS state")
