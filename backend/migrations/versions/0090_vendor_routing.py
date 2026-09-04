"""供应商路由决策与 attempt 账本；uncertain 禁止自动切换。"""

from __future__ import annotations

from alembic import op

revision = "0090_vendor_routing"
down_revision = "0089_send_admission_metrics_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS selected_vendor VARCHAR(32)
            NOT NULL DEFAULT 'zhihui'
        """
    )
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS routing_reason VARCHAR(64)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS route_policy_version SMALLINT
            NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sms_batch'::regclass
              AND conname='ck_sms_batch_selected_vendor'
          ) THEN
            ALTER TABLE sms_batch
              ADD CONSTRAINT ck_sms_batch_selected_vendor
              CHECK (selected_vendor ~ '^[a-z][a-z0-9_]{0,31}$');
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sms_batch'::regclass
              AND conname='ck_sms_batch_route_policy_version'
          ) THEN
            ALTER TABLE sms_batch
              ADD CONSTRAINT ck_sms_batch_route_policy_version
              CHECK (route_policy_version >= 1);
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS selected_vendor VARCHAR(32)
            NOT NULL DEFAULT 'zhihui'
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS route_generation INTEGER
            NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sms_chunk'::regclass
              AND conname='ck_sms_chunk_selected_vendor'
          ) THEN
            ALTER TABLE sms_chunk
              ADD CONSTRAINT ck_sms_chunk_selected_vendor
              CHECK (selected_vendor ~ '^[a-z][a-z0-9_]{0,31}$');
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sms_chunk'::regclass
              AND conname='ck_sms_chunk_route_generation'
          ) THEN
            ALTER TABLE sms_chunk
              ADD CONSTRAINT ck_sms_chunk_route_generation
              CHECK (route_generation >= 1);
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS vendor_id VARCHAR(32)
            NOT NULL DEFAULT 'zhihui'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='raw_vendor_log'::regclass
              AND conname='ck_raw_vendor_log_vendor_id'
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT ck_raw_vendor_log_vendor_id
              CHECK (vendor_id ~ '^[a-z][a-z0-9_]{0,31}$');
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_vendor_attempt (
            id                 BIGSERIAL PRIMARY KEY,
            chunk_id           BIGINT      NOT NULL REFERENCES sms_chunk(id),
            vendor_id          VARCHAR(32) NOT NULL
                               CHECK (vendor_id ~ '^[a-z][a-z0-9_]{0,31}$'),
            generation         INTEGER     NOT NULL CHECK (generation >= 1),
            outcome            VARCHAR(24) NOT NULL
                               CHECK (outcome IN (
                                 'not_invoked','rejected','submitted','uncertain','failed',
                                 'retry_scheduled','delayed','paused','stale'
                               )),
            safe_to_failover   BOOLEAN     NOT NULL DEFAULT FALSE,
            vendor_code        INTEGER,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (chunk_id, vendor_id, generation)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_sms_vendor_attempt_irreversible
          ON sms_vendor_attempt (chunk_id)
          WHERE outcome IN ('submitted','uncertain')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sms_vendor_attempt_chunk
          ON sms_vendor_attempt (chunk_id, created_at DESC)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON sms_vendor_attempt TO sms_send")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE sms_vendor_attempt_id_seq TO sms_send")
    op.execute(
        """
        GRANT SELECT (outcome, created_at)
          ON sms_vendor_attempt TO sms_metrics
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sms_vendor_attempt")
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_log_vendor_id"
    )
    op.execute("ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS vendor_id")
    op.execute(
        "ALTER TABLE sms_chunk DROP CONSTRAINT IF EXISTS ck_sms_chunk_selected_vendor"
    )
    op.execute(
        "ALTER TABLE sms_chunk DROP CONSTRAINT IF EXISTS ck_sms_chunk_route_generation"
    )
    op.execute("ALTER TABLE sms_chunk DROP COLUMN IF EXISTS selected_vendor")
    op.execute("ALTER TABLE sms_chunk DROP COLUMN IF EXISTS route_generation")
    op.execute(
        "ALTER TABLE sms_batch DROP CONSTRAINT IF EXISTS ck_sms_batch_selected_vendor"
    )
    op.execute(
        "ALTER TABLE sms_batch DROP CONSTRAINT IF EXISTS ck_sms_batch_route_policy_version"
    )
    op.execute("ALTER TABLE sms_batch DROP COLUMN IF EXISTS selected_vendor")
    op.execute("ALTER TABLE sms_batch DROP COLUMN IF EXISTS routing_reason")
    op.execute("ALTER TABLE sms_batch DROP COLUMN IF EXISTS route_policy_version")
