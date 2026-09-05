"""在途容量以 reservation 明细为权威，balance 必须等于活动求和。"""

from __future__ import annotations

from alembic import op

revision = "0101_inflight_balance_conservation"
down_revision = "0100_inflight_acceptance_failed_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE send_inflight_balance
          ADD COLUMN IF NOT EXISTS conservation_blocked_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS send_inflight_reconcile_fact (
            id BIGSERIAL PRIMARY KEY,
            app_id BIGINT NOT NULL REFERENCES app(id) ON DELETE RESTRICT,
            stored_balance INTEGER,
            computed_active_sum INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            active_reservation_count INTEGER NOT NULL,
            result VARCHAR(16) NOT NULL,
            CONSTRAINT ck_send_inflight_reconcile_result
              CHECK (result IN ('matched', 'repaired', 'blocked')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION send_inflight_active_states()
        RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $$
          SELECT ARRAY['reserved', 'batch_bound', 'materialized']::text[];
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_send_inflight_balance_conservation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        DECLARE
          target_app BIGINT;
          expected INTEGER;
          stored INTEGER;
        BEGIN
          target_app := COALESCE(NEW.app_id, OLD.app_id);
          SELECT COALESCE(SUM(reserved_chunks), 0)::integer
            INTO expected
            FROM send_inflight_reservation
           WHERE app_id = target_app
             AND state = ANY (send_inflight_active_states());
          SELECT reserved_chunks
            INTO stored
            FROM send_inflight_balance
           WHERE app_id = target_app;
          IF stored IS NULL THEN
            IF expected <> 0 THEN
              RAISE EXCEPTION 'send_inflight balance missing'
                USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
          END IF;
          IF stored <> expected THEN
            RAISE EXCEPTION 'send_inflight balance drift'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION check_send_inflight_balance_conservation() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reconcile_send_inflight_app(p_app_id BIGINT)
        RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        DECLARE
          computed INTEGER;
          active_count INTEGER;
          stored INTEGER;
          blocked_at TIMESTAMPTZ;
          dupes INTEGER;
        BEGIN
          PERFORM pg_advisory_xact_lock(868632, p_app_id);
          SELECT COUNT(*) INTO dupes
            FROM (
              SELECT batch_id
                FROM send_inflight_reservation
               WHERE app_id = p_app_id
                 AND state = ANY (send_inflight_active_states())
                 AND batch_id IS NOT NULL
               GROUP BY batch_id
              HAVING COUNT(*) > 1
            ) d;
          SELECT COALESCE(SUM(reserved_chunks), 0)::integer, COUNT(*)::integer
            INTO computed, active_count
            FROM send_inflight_reservation
           WHERE app_id = p_app_id
             AND state = ANY (send_inflight_active_states());
          SELECT reserved_chunks, conservation_blocked_at
            INTO stored, blocked_at
            FROM send_inflight_balance
           WHERE app_id = p_app_id
           FOR UPDATE;
          IF dupes > 0 THEN
            IF FOUND THEN
              UPDATE send_inflight_balance
                 SET conservation_blocked_at = COALESCE(conservation_blocked_at, now()),
                     updated_at = now()
               WHERE app_id = p_app_id;
            END IF;
            INSERT INTO send_inflight_reconcile_fact(
              app_id, stored_balance, computed_active_sum, delta,
              active_reservation_count, result
            ) VALUES (
              p_app_id, stored, computed, computed - COALESCE(stored, 0),
              active_count, 'blocked'
            );
            RETURN 'blocked';
          END IF;
          IF NOT FOUND THEN
            IF computed = 0 THEN
              RETURN 'matched';
            END IF;
            INSERT INTO send_inflight_balance(app_id, reserved_chunks)
            VALUES (p_app_id, computed);
            INSERT INTO send_inflight_reconcile_fact(
              app_id, stored_balance, computed_active_sum, delta,
              active_reservation_count, result
            ) VALUES (p_app_id, NULL, computed, computed, active_count, 'repaired');
            RETURN 'repaired';
          END IF;
          IF stored = computed THEN
            IF blocked_at IS NOT NULL THEN
              UPDATE send_inflight_balance
                 SET conservation_blocked_at = NULL,
                     updated_at = now()
               WHERE app_id = p_app_id;
            END IF;
            RETURN 'matched';
          END IF;
          UPDATE send_inflight_balance
             SET reserved_chunks = computed,
                 conservation_blocked_at = NULL,
                 updated_at = now()
           WHERE app_id = p_app_id;
          INSERT INTO send_inflight_reconcile_fact(
            app_id, stored_balance, computed_active_sum, delta,
            active_reservation_count, result
          ) VALUES (
            p_app_id, stored, computed, computed - stored, active_count, 'repaired'
          );
          RETURN 'repaired';
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION reconcile_send_inflight_app(BIGINT) FROM PUBLIC")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON send_inflight_reconcile_fact
          TO sms_accept, sms_send
        """
    )
    op.execute(
        """
        GRANT USAGE, SELECT ON SEQUENCE send_inflight_reconcile_fact_id_seq
          TO sms_accept, sms_send
        """
    )
    op.execute(
        """
        GRANT SELECT (app_id, reserved_chunks, conservation_blocked_at)
          ON send_inflight_balance TO sms_metrics
        """
    )
    op.execute(
        """
        GRANT SELECT (app_id, stored_balance, computed_active_sum, delta,
                      active_reservation_count, result, created_at)
          ON send_inflight_reconcile_fact TO sms_metrics
        """
    )
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION send_inflight_active_states()
          TO sms_accept, sms_send, sms_scheduler, sms_metrics
        """
    )
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION reconcile_send_inflight_app(BIGINT)
          TO sms_accept, sms_send
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_send_inflight_reservation_conservation
          ON send_inflight_reservation
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_send_inflight_reservation_conservation
        AFTER INSERT OR UPDATE OR DELETE ON send_inflight_reservation
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_send_inflight_balance_conservation()
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_send_inflight_balance_conservation
          ON send_inflight_balance
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_send_inflight_balance_conservation
        AFTER INSERT OR UPDATE OR DELETE ON send_inflight_balance
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_send_inflight_balance_conservation()
        """
    )


def downgrade() -> None:
    # 回滚不得删除守恒触发器、对账函数与失败关闭列（D105 / #632）。
    return
