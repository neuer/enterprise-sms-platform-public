"""为 callback/export 增加可续租 UUID fencing 与稳定回调事件 ID。"""

from __future__ import annotations

from alembic import op

revision = "0029_worker_fencing_leases"
down_revision = "0028_usage_fact_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展租约字段并把旧时间戳租约安全转换为可接管状态。"""

    op.execute(
        """
        ALTER TABLE callback_task
          ADD COLUMN IF NOT EXISTS event_id UUID,
          ADD COLUMN IF NOT EXISTS lease_id UUID,
          ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS takeover_count INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute("UPDATE callback_task SET event_id=gen_random_uuid() WHERE event_id IS NULL")
    op.execute("ALTER TABLE callback_task ALTER COLUMN event_id SET DEFAULT gen_random_uuid()")
    op.execute("ALTER TABLE callback_task ALTER COLUMN event_id SET NOT NULL")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_callback_task_event_id
          ON callback_task(event_id)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='chk_cb_takeover_count'
          ) THEN
            ALTER TABLE callback_task ADD CONSTRAINT chk_cb_takeover_count
              CHECK (takeover_count>=0);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='chk_cb_lease_pair'
          ) THEN
            ALTER TABLE callback_task ADD CONSTRAINT chk_cb_lease_pair CHECK (
              (lease_id IS NULL AND lease_expires_at IS NULL)
              OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
            );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='chk_cb_lease_state'
          ) THEN
            ALTER TABLE callback_task ADD CONSTRAINT chk_cb_lease_state CHECK (
              lease_id IS NULL OR status='retrying'
            );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cb_lease_expiry
          ON callback_task(lease_expires_at,id) WHERE lease_id IS NOT NULL
        """
    )

    op.execute(
        """
        ALTER TABLE export_task
          ADD COLUMN IF NOT EXISTS lease_id UUID,
          ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS takeover_count INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE export_task
        SET lease_id=gen_random_uuid(),
            lease_expires_at=COALESCE(started_at,now())+interval '15 minutes'
        WHERE status='running' AND lease_id IS NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='chk_export_takeover_count'
          ) THEN
            ALTER TABLE export_task ADD CONSTRAINT chk_export_takeover_count
              CHECK (takeover_count>=0);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='chk_export_lease_pair'
          ) THEN
            ALTER TABLE export_task ADD CONSTRAINT chk_export_lease_pair CHECK (
              (lease_id IS NULL AND lease_expires_at IS NULL)
              OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
            );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='chk_export_lease_state'
          ) THEN
            ALTER TABLE export_task ADD CONSTRAINT chk_export_lease_state CHECK (
              lease_id IS NULL OR status='running'
            );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_export_lease_expiry
          ON export_task(lease_expires_at,id) WHERE lease_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_lease_event (
          id BIGSERIAL PRIMARY KEY,
          task_kind VARCHAR(16) NOT NULL
            CHECK (task_kind IN ('callback','export')),
          task_id BIGINT NOT NULL CHECK (task_id>0),
          event_type VARCHAR(24) NOT NULL
            CHECK (event_type IN (
              'acquired','takeover','heartbeat_lost',
              'fencing_miss','dead','manual_retry'
            )),
          lease_id UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_lease_event_metrics
          ON worker_lease_event(task_kind,event_type,created_at)
        """
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON worker_lease_event FROM sms_app")
    op.execute("GRANT SELECT, INSERT ON worker_lease_event TO sms_app")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE worker_lease_event_id_seq TO sms_app"
    )


def downgrade() -> None:
    """仅在没有活动租约/证据时允许回退，避免恢复语义静默丢失。"""

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM callback_task WHERE lease_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM export_task WHERE lease_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM worker_lease_event) THEN
            RAISE EXCEPTION 'cannot downgrade worker fencing with lease evidence';
          END IF;
        END $$;
        """
    )
    op.execute("DROP TABLE worker_lease_event")
    op.execute("DROP INDEX IF EXISTS idx_export_lease_expiry")
    op.execute("ALTER TABLE export_task DROP CONSTRAINT IF EXISTS chk_export_lease_state")
    op.execute("ALTER TABLE export_task DROP CONSTRAINT IF EXISTS chk_export_lease_pair")
    op.execute("ALTER TABLE export_task DROP CONSTRAINT IF EXISTS chk_export_takeover_count")
    op.execute("ALTER TABLE export_task DROP COLUMN takeover_count")
    op.execute("ALTER TABLE export_task DROP COLUMN lease_expires_at")
    op.execute("ALTER TABLE export_task DROP COLUMN lease_id")
    op.execute("DROP INDEX IF EXISTS idx_cb_lease_expiry")
    op.execute("DROP INDEX IF EXISTS uk_callback_task_event_id")
    op.execute("ALTER TABLE callback_task DROP CONSTRAINT IF EXISTS chk_cb_lease_state")
    op.execute("ALTER TABLE callback_task DROP CONSTRAINT IF EXISTS chk_cb_lease_pair")
    op.execute("ALTER TABLE callback_task DROP CONSTRAINT IF EXISTS chk_cb_takeover_count")
    op.execute("ALTER TABLE callback_task DROP COLUMN takeover_count")
    op.execute("ALTER TABLE callback_task DROP COLUMN lease_expires_at")
    op.execute("ALTER TABLE callback_task DROP COLUMN lease_id")
    op.execute("ALTER TABLE callback_task DROP COLUMN event_id")
