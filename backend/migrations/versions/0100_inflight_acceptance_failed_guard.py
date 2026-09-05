"""acceptance-failed 不得释放已绑定批次的在途预留。"""

from __future__ import annotations

from alembic import op

revision = "0100_inflight_acceptance_failed_guard"
down_revision = "0099_idempotency_claim_lease_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 已装入当前 schema.sql 的 DEFERRABLE 守恒触发器时，同事务里
    # 0094 回填会留下 pending trigger events，必须先立即校验再 ALTER。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          DROP CONSTRAINT IF EXISTS ck_send_inflight_acceptance_failed_unbound
        """
    )
    op.execute(
        """
        ALTER TABLE send_inflight_reservation
          ADD CONSTRAINT ck_send_inflight_acceptance_failed_unbound
          CHECK (
            release_reason IS DISTINCT FROM 'acceptance-failed'
            OR batch_id IS NULL
          )
          NOT VALID
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_bound_acceptance_failed()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        BEGIN
          IF NEW.release_reason IS NOT DISTINCT FROM 'acceptance-failed'
             AND (
               NEW.batch_id IS NOT NULL
               OR OLD.batch_id IS NOT NULL
               OR OLD.state IN ('batch_bound', 'materialized')
             ) THEN
            RAISE EXCEPTION
              'acceptance-failed cannot release a bound reservation'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION reject_bound_acceptance_failed() FROM PUBLIC")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_send_inflight_reject_bound_acceptance_failed "
        "ON send_inflight_reservation"
    )
    op.execute(
        """
        CREATE TRIGGER trg_send_inflight_reject_bound_acceptance_failed
        BEFORE UPDATE ON send_inflight_reservation
        FOR EACH ROW
        EXECUTE FUNCTION reject_bound_acceptance_failed()
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_send_inflight_misreleased_acceptance
          ON send_inflight_reservation (app_id)
          WHERE state='released'
            AND release_reason='acceptance-failed'
            AND batch_id IS NOT NULL
        """
    )


def downgrade() -> None:
    # 回滚不得删除防误释放约束与触发器（D104 / #628）。
    return
