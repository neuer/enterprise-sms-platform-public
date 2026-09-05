"""在途预留补齐绑定、校准与释放生命周期。"""

from __future__ import annotations

from alembic import op

revision = "0094_send_inflight_reservation_lifecycle"
down_revision = "0093_auth_r2_credential_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD COLUMN IF NOT EXISTS generation INTEGER NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_send_inflight_reservation_generation
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD CONSTRAINT ck_send_inflight_reservation_generation
          CHECK (generation >= 1)
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD COLUMN IF NOT EXISTS materialized_chunks INTEGER
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_send_inflight_materialized_chunks
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD CONSTRAINT ck_send_inflight_materialized_chunks
          CHECK (materialized_chunks IS NULL OR materialized_chunks >= 0)
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD COLUMN IF NOT EXISTS release_reason VARCHAR(32)
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD COLUMN IF NOT EXISTS bound_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD COLUMN IF NOT EXISTS materialized_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS send_inflight_reservation_state_check
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD CONSTRAINT send_inflight_reservation_state_check
          CHECK (state IN ('reserved','batch_bound','materialized','released'))
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_inflight_released_pair
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD CONSTRAINT ck_inflight_released_pair CHECK (
            (state = 'released') = (released_at IS NOT NULL AND release_reason IS NOT NULL)
          )
        """
    )
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS send_inflight_reservation_id BIGINT
          REFERENCES send_inflight_reservation(id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_sms_batch_send_inflight_reservation
          ON sms_batch(send_inflight_reservation_id)
          WHERE send_inflight_reservation_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_send_inflight_reservation_active
          ON send_inflight_reservation(app_id, state, expires_at)
          WHERE state <> 'released'
        """
    )
    op.execute(
        """
        INSERT INTO send_inflight_reservation (
          app_id, batch_id, reserved_chunks, state, generation, bound_at
        )
        SELECT
          b.app_id,
          b.id,
          GREATEST(
            1,
            COALESCE(
              NULLIF((SELECT count(*) FROM sms_chunk c WHERE c.batch_id=b.id), 0),
              CEIL(GREATEST(b.total, 1)::numeric / 500.0)
            )::integer
          ),
          'batch_bound',
          1,
          now()
        FROM sms_batch b
        WHERE b.app_id IS NOT NULL
          AND b.status IN (
            'pending_approval','scheduled','queued','sending','balance_blocked'
          )
          AND NOT EXISTS (
            SELECT 1 FROM send_inflight_reservation r WHERE r.batch_id=b.id
          )
        """
    )
    op.execute(
        """
        UPDATE sms_batch b
        SET send_inflight_reservation_id=r.id
        FROM send_inflight_reservation r
        WHERE r.batch_id=b.id
          AND b.send_inflight_reservation_id IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO send_inflight_balance(app_id, reserved_chunks, updated_at)
        SELECT r.app_id, 0, now()
        FROM send_inflight_reservation r
        WHERE r.state <> 'released'
        GROUP BY r.app_id
        ON CONFLICT (app_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE send_inflight_balance b
        SET reserved_chunks=COALESCE((
              SELECT SUM(r.reserved_chunks)
              FROM send_inflight_reservation r
              WHERE r.app_id=b.app_id AND r.state <> 'released'
            ), 0),
            updated_at=now()
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_send_inflight_reservation_active")
    op.execute("DROP INDEX IF EXISTS uk_sms_batch_send_inflight_reservation")
    op.execute(
        """
        ALTER TABLE sms_batch
          DROP COLUMN IF EXISTS send_inflight_reservation_id
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_inflight_released_pair
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS send_inflight_reservation_state_check
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD CONSTRAINT send_inflight_reservation_state_check
          CHECK (state IN ('reserved','materialized','released'))
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_send_inflight_materialized_chunks
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_send_inflight_reservation_generation
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP COLUMN IF EXISTS materialized_at,
          DROP COLUMN IF EXISTS bound_at,
          DROP COLUMN IF EXISTS release_reason,
          DROP COLUMN IF EXISTS expires_at,
          DROP COLUMN IF EXISTS materialized_chunks,
          DROP COLUMN IF EXISTS generation
        """
    )
