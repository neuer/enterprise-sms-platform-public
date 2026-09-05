"""动态拆分必须同步扩充在途预留，容量不足进入可恢复阻塞态。"""

from __future__ import annotations

from alembic import op

revision = "0102_inflight_split_capacity"
down_revision = "0101_inflight_balance_conservation"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            'uncertain','unknown_terminal','split_capacity_blocked'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS parent_chunk_id BIGINT
            REFERENCES sms_chunk(id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS split_generation INTEGER
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS child_ordinal SMALLINT
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          DROP CONSTRAINT IF EXISTS ck_sms_chunk_split_identity
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD CONSTRAINT ck_sms_chunk_split_identity CHECK (
            (
              parent_chunk_id IS NULL
              AND split_generation IS NULL
              AND child_ordinal IS NULL
            ) OR (
              parent_chunk_id IS NOT NULL
              AND split_generation >= 1
              AND child_ordinal IN (1, 2)
            )
          )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_sms_chunk_split_child
          ON sms_chunk(parent_chunk_id, split_generation, child_ordinal)
          WHERE parent_chunk_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_split_capacity_blocked
          ON sms_chunk(id) WHERE status = 'split_capacity_blocked'
        """
    )
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
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION send_chunk_occupying_states()
          TO sms_accept, sms_send, sms_scheduler, sms_metrics
        """
    )


def downgrade() -> None:
    # 回滚不得删除拆分身份、占用函数与阻塞态（D106 / #631）。
    return
