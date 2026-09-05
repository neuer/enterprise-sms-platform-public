"""动态拆分必须同步扩充在途预留，容量不足进入可恢复阻塞态。"""

from __future__ import annotations

from alembic import op

revision = "0102_inflight_split_capacity"
down_revision = "0101_inflight_balance_conservation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 已装入占用延迟触发器时，同事务里对 sms_chunk 的回填会留下
    # pending trigger events；先立即校验再 ALTER。
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_send_inflight_chunk_occupancy()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        DECLARE
          target_batch BIGINT;
          reserved INTEGER;
          occupying INTEGER;
        BEGIN
          IF TG_TABLE_NAME = 'sms_chunk' THEN
            IF TG_OP = 'UPDATE'
               AND NEW.status IS NOT DISTINCT FROM OLD.status
               AND NEW.batch_id IS NOT DISTINCT FROM OLD.batch_id THEN
              RETURN NULL;
            END IF;
            target_batch := COALESCE(NEW.batch_id, OLD.batch_id);
          ELSE
            IF TG_OP = 'DELETE' THEN
              RETURN NULL;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.reserved_chunks IS NOT DISTINCT FROM OLD.reserved_chunks
               AND NEW.state IS NOT DISTINCT FROM OLD.state
               AND NEW.batch_id IS NOT DISTINCT FROM OLD.batch_id THEN
              RETURN NULL;
            END IF;
            IF NOT (NEW.state = ANY (send_inflight_active_states())) THEN
              RETURN NULL;
            END IF;
            target_batch := NEW.batch_id;
          END IF;
          IF target_batch IS NULL THEN
            RETURN NULL;
          END IF;
          SELECT reserved_chunks
            INTO reserved
            FROM send_inflight_reservation
           WHERE batch_id = target_batch
             AND state = ANY (send_inflight_active_states());
          IF reserved IS NULL THEN
            RETURN NULL;
          END IF;
          SELECT COUNT(*)::integer
            INTO occupying
            FROM sms_chunk
           WHERE batch_id = target_batch
             AND status = ANY (send_chunk_occupying_states());
          IF occupying > reserved THEN
            RAISE EXCEPTION 'send_inflight occupancy exceeds reservation'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION check_send_inflight_chunk_occupancy() FROM PUBLIC"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sms_chunk_inflight_occupancy ON sms_chunk"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_sms_chunk_inflight_occupancy
        AFTER INSERT OR UPDATE OR DELETE ON sms_chunk
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_send_inflight_chunk_occupancy()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_send_inflight_reservation_occupancy "
        "ON send_inflight_reservation"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_send_inflight_reservation_occupancy
        AFTER INSERT OR UPDATE OR DELETE ON send_inflight_reservation
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_send_inflight_chunk_occupancy()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_sms_chunk_split_children_complete()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        DECLARE
          target_parent BIGINT;
          target_generation INTEGER;
          child_count INTEGER;
        BEGIN
          target_parent := COALESCE(NEW.parent_chunk_id, OLD.parent_chunk_id);
          target_generation := COALESCE(NEW.split_generation, OLD.split_generation);
          IF target_parent IS NULL OR target_generation IS NULL THEN
            RETURN NULL;
          END IF;
          SELECT COUNT(*)::integer
            INTO child_count
            FROM sms_chunk
           WHERE parent_chunk_id = target_parent
             AND split_generation = target_generation;
          IF child_count <> 0 AND child_count <> 2 THEN
            RAISE EXCEPTION 'sms_chunk split children incomplete'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION check_sms_chunk_split_children_complete() FROM PUBLIC"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sms_chunk_split_children_complete ON sms_chunk"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_sms_chunk_split_children_complete
        AFTER INSERT OR UPDATE OR DELETE ON sms_chunk
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_sms_chunk_split_children_complete()
        """
    )


def downgrade() -> None:
    # 回滚不得删除拆分身份、占用函数与阻塞态（D106 / #631）。
    return
