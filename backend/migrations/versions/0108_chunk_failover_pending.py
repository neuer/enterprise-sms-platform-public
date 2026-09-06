"""safe reject 后持久化 failover_pending，禁止留下 rejected+submitting。"""

from __future__ import annotations

from alembic import op

revision = "0108_chunk_failover_pending"
down_revision = "0107_report_timeout_sweep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        ALTER TABLE sms_chunk
          ALTER COLUMN status TYPE VARCHAR(32)
        """
    )
    op.execute("ALTER TABLE sms_chunk DROP CONSTRAINT IF EXISTS sms_chunk_status_check")
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD CONSTRAINT sms_chunk_status_check CHECK (status IN (
            'pending','submitting','submitted','failed','retrying',
            'uncertain','unknown_terminal','split_capacity_blocked',
            'failover_pending'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS next_vendor VARCHAR(32)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS route_policy_version SMALLINT
            NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS failover_from_attempt_id BIGINT
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sms_chunk'::regclass
              AND conname='ck_sms_chunk_next_vendor'
          ) THEN
            ALTER TABLE sms_chunk
              ADD CONSTRAINT ck_sms_chunk_next_vendor
              CHECK (next_vendor IS NULL OR next_vendor ~ '^[a-z][a-z0-9_]{0,31}$');
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sms_chunk'::regclass
              AND conname='ck_sms_chunk_route_policy_version'
          ) THEN
            ALTER TABLE sms_chunk
              ADD CONSTRAINT ck_sms_chunk_route_policy_version
              CHECK (route_policy_version >= 1);
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_failover_due
          ON sms_chunk(retry_not_before) WHERE status = 'failover_pending'
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION send_chunk_occupying_states()
        RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $$
          SELECT ARRAY[
            'pending','submitting','retrying','submitted',
            'uncertain','split_capacity_blocked','failover_pending'
          ]::text[];
        $$
        """
    )
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION send_chunk_occupying_states()
          TO sms_accept, sms_send, sms_scheduler, sms_metrics
        """
    )


def downgrade() -> None:
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        UPDATE sms_chunk SET status='submitting'
        WHERE status='failover_pending'
        """
    )
    op.execute("ALTER TABLE sms_chunk DROP CONSTRAINT IF EXISTS sms_chunk_status_check")
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD CONSTRAINT sms_chunk_status_check CHECK (status IN (
            'pending','submitting','submitted','failed','retrying',
            'uncertain','unknown_terminal','split_capacity_blocked'
          ))
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_chunk_failover_due")
    op.execute(
        "ALTER TABLE sms_chunk DROP CONSTRAINT IF EXISTS ck_sms_chunk_next_vendor"
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          DROP CONSTRAINT IF EXISTS ck_sms_chunk_route_policy_version
        """
    )
    op.execute("ALTER TABLE sms_chunk DROP COLUMN IF EXISTS failover_from_attempt_id")
    op.execute("ALTER TABLE sms_chunk DROP COLUMN IF EXISTS route_policy_version")
    op.execute("ALTER TABLE sms_chunk DROP COLUMN IF EXISTS next_vendor")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION send_chunk_occupying_states()
        RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $$
          SELECT ARRAY[
            'pending','submitting','retrying','submitted',
            'uncertain','split_capacity_blocked'
          ]::text[];
        $$
        """
    )
