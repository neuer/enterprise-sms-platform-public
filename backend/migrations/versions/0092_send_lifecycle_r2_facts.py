"""人工处置可恢复 effect、chunk 额度事实、在途预留与 admission epoch。"""

from __future__ import annotations

from alembic import op

revision = "0092_send_lifecycle_r2_facts"
down_revision = "0091_api_key_digest_algorithms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ALTER COLUMN state TYPE VARCHAR(32)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS effect_generation INTEGER NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS effect_error VARCHAR(128)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS source_app_id BIGINT REFERENCES app(id)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS source_channel VARCHAR(8)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS source_category VARCHAR(16)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS effect_applied_at TIMESTAMPTZ
        """
    )
    op.execute(
        "ALTER TABLE sms_uncertain_resolution "
        "DROP CONSTRAINT IF EXISTS sms_uncertain_resolution_state_check"
    )
    op.execute(
        "ALTER TABLE sms_uncertain_resolution "
        "DROP CONSTRAINT IF EXISTS ck_uncertain_resolution_confirm_pair"
    )
    op.execute(
        """
        UPDATE sms_uncertain_resolution
           SET state='effect_applied',
               approved_at=COALESCE(approved_at, confirmed_at),
               effect_applied_at=COALESCE(effect_applied_at, confirmed_at)
         WHERE state='confirmed'
           AND action='resend_new_batch'
           AND child_batch_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE sms_uncertain_resolution
           SET state='retryable_effect_error',
               approved_at=COALESCE(approved_at, confirmed_at),
               effect_error='child_batch_missing'
         WHERE state='confirmed'
           AND action='resend_new_batch'
           AND child_batch_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE sms_uncertain_resolution
           SET state='closed',
               approved_at=COALESCE(approved_at, confirmed_at),
               effect_applied_at=COALESCE(effect_applied_at, confirmed_at)
         WHERE state='confirmed'
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD CONSTRAINT sms_uncertain_resolution_state_check CHECK (
            state IN (
              'proposed','approved','effect_pending','applying','effect_applied',
              'closed','approval_rejected','retryable_effect_error',
              'manual_intervention_required','cancelled_before_effect'
            )
          )
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD CONSTRAINT ck_uncertain_resolution_confirm_pair CHECK (
            (state='proposed' AND confirmer_account_id IS NULL AND confirmed_at IS NULL)
            OR (
              state IN (
                'approved','effect_pending','applying','effect_applied','closed',
                'retryable_effect_error','manual_intervention_required'
              )
              AND confirmer_account_id IS NOT NULL
              AND confirmed_at IS NOT NULL
            )
            OR state IN ('approval_rejected','cancelled_before_effect')
          )
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD CONSTRAINT ck_uncertain_resolution_effect_generation
          CHECK (effect_generation >= 1)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_uncertain_child (
          resolution_id BIGINT PRIMARY KEY
            REFERENCES sms_uncertain_resolution(id) ON DELETE RESTRICT,
          child_batch_id BIGINT NOT NULL UNIQUE
            REFERENCES sms_batch(id) ON DELETE RESTRICT,
          generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_chunk_allocation (
          chunk_id BIGINT PRIMARY KEY REFERENCES sms_chunk(id) ON DELETE RESTRICT,
          batch_id BIGINT NOT NULL REFERENCES sms_batch(id) ON DELETE RESTRICT,
          reservation_id UUID REFERENCES usage_reservation(id),
          recipient_count INTEGER NOT NULL CHECK (recipient_count >= 0),
          segment_count INTEGER NOT NULL CHECK (segment_count >= 0),
          request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
          app_id BIGINT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_chunk_release (
          resolution_id BIGINT PRIMARY KEY
            REFERENCES sms_uncertain_resolution(id) ON DELETE RESTRICT,
          chunk_id BIGINT NOT NULL REFERENCES sms_chunk(id) ON DELETE RESTRICT,
          reservation_id UUID,
          recipient_count INTEGER NOT NULL CHECK (recipient_count >= 0),
          segment_count INTEGER NOT NULL CHECK (segment_count >= 0),
          request_count INTEGER NOT NULL CHECK (request_count >= 0),
          release_event_id VARCHAR(80) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT ck_usage_chunk_release_event CHECK (
            release_event_id ~ '^resolution[:][1-9][0-9]*[:]not-accepted$'
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS send_inflight_balance (
          app_id BIGINT PRIMARY KEY REFERENCES app(id) ON DELETE RESTRICT,
          reserved_chunks INTEGER NOT NULL DEFAULT 0 CHECK (reserved_chunks >= 0),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS send_inflight_reservation (
          id BIGSERIAL PRIMARY KEY,
          app_id BIGINT NOT NULL REFERENCES app(id) ON DELETE RESTRICT,
          batch_id BIGINT UNIQUE REFERENCES sms_batch(id),
          reserved_chunks INTEGER NOT NULL CHECK (reserved_chunks >= 1),
          state VARCHAR(16) NOT NULL DEFAULT 'reserved'
            CHECK (state IN ('reserved','materialized','released')),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          released_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS send_admission_state (
          scope VARCHAR(16) PRIMARY KEY,
          state VARCHAR(16) NOT NULL
            CHECK (state IN ('open','degraded','closed')),
          reason_code VARCHAR(32) NOT NULL,
          state_epoch BIGINT NOT NULL DEFAULT 1 CHECK (state_epoch >= 1),
          hold_until TIMESTAMPTZ,
          valid_until TIMESTAMPTZ NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO send_admission_state (
          scope, state, reason_code, state_epoch, valid_until
        ) VALUES (
          'send', 'closed', 'bootstrap', 1, now()
        )
        ON CONFLICT (scope) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS send_runtime_heartbeat (
          component VARCHAR(32) PRIMARY KEY,
          generation BIGINT NOT NULL DEFAULT 1 CHECK (generation >= 1),
          last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_success_at TIMESTAMPTZ,
          lease_until TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          DROP CONSTRAINT IF EXISTS ck_usage_release_event_no_pii
        """
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          ADD CONSTRAINT ck_usage_release_event_no_pii CHECK (
            release_event_id IS NULL
            OR release_event_id ~ (
              '^(batch[:][0-9a-f]{32}[:]cancelled|'
              ||'approval[:][1-9][0-9]*[:](rejected|expired)|usage[:]'
              ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              ||'[89ab][0-9a-f]{3}-[0-9a-f]{12}[:]'
              ||'(acceptance-failed|all-filtered|idempotent-reuse|'
              ||'orphan-recovery|uncertain-retry|uncertain-unused))$'
            )
          )
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_task_name")
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_args_no_pii")
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_refs_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_task_name CHECK (task_name IN (
          'app.tasks.bind_sign',
          'app.tasks.bind_template',
          'app.tasks.adopt_sign',
          'app.tasks.sync_template',
          'app.tasks.send.process_batch',
          'app.tasks.send.process_chunk',
          'app.tasks.deliver_callback',
          'app.tasks.outbox.compensate_quota',
          'app.tasks.outbox.deliver_alert',
          'app.tasks.outbox.release_usage',
          'app.tasks.outbox.trigger_job',
          'app.tasks.outbox.apply_uncertain_effect'
        ))
        """
    )
    op.execute(
        r"""
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_args_no_pii CHECK (
          (
            args::text !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name IN (
                'app.tasks.bind_sign',
                'app.tasks.bind_template',
                'app.tasks.sync_template'
              )
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[1-9][0-9]*$'
            )
            OR (
              task_name='app.tasks.adopt_sign'
              AND jsonb_array_length(args)=2
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[1-9][0-9]*$'
              AND jsonb_typeof(args->1)='number'
              AND args->>1 ~ '^[1-9][0-9]*$'
              AND (args->>1)::bigint <= 2147483647
            )
            OR (
              task_name='app.tasks.send.process_batch'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name IN (
                'app.tasks.send.process_chunk',
                'app.tasks.outbox.apply_uncertain_effect'
              )
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[1-9][0-9]*$'
            )
            OR (
              task_name='app.tasks.deliver_callback'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.deliver_alert'
              AND jsonb_array_length(args)=2
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND args->>1 IN ('wecom','smtp')
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND jsonb_array_length(args)=6
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND jsonb_typeof(args->1)='string'
              AND args->>1 !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              AND args->>2 IN ('verify','notice','market')
              AND args->>3 ~ '^[0-9]{8}$'
              AND jsonb_typeof(args->4)='number'
              AND args->>4 ~ '^[0-9]+$'
              AND args->>5 ~ (
                '^(batch[:][0-9a-f]{32}[:]cancelled|'
                || 'approval[:][1-9][0-9]*[:](rejected|expired))$'
              )
            )
            OR (
              task_name='app.tasks.outbox.release_usage'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ (
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
                || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              )
            )
          )
          AND args::text !~ (
            '"(phone|phones|mobile|mobiles|phone_enc|phone_hmac|'
            || 'content|body|secret|password)"[[:space:]]*:'
          )
        )
        """
    )
    op.execute(
        r"""
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_refs_no_pii CHECK (
          (
            aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name IN (
                'app.tasks.bind_sign',
                'app.tasks.bind_template',
                'app.tasks.adopt_sign',
                'app.tasks.sync_template',
                'app.tasks.outbox.apply_uncertain_effect'
              )
              AND aggregate_id ~ '^[1-9][0-9]*$'
            )
            OR (
              task_name='app.tasks.send.process_batch'
              AND aggregate_id ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.send.process_chunk'
              AND aggregate_id ~ '^[1-9][0-9]*$'
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND aggregate_id ~ '^([0-9a-f]{32}|[0-9]+)$'
            )
            OR (
              task_name IN (
                'app.tasks.deliver_callback',
                'app.tasks.outbox.deliver_alert'
              )
              AND aggregate_id ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.release_usage'
              AND aggregate_id ~ (
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
                || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              )
            )
          )
          AND (
            dedup_key !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR dedup_key ~ (
              '^(batch[.]ready[:][0-9a-f]{32}|'
              || 'chunk[.]ready[:][1-9][0-9]*|'
              || 'uncertain[.]effect[:][1-9][0-9]*[:][1-9][0-9]*|'
              || 'scheduled[:][0-9a-f]{32}[:]ready|'
              || 'batch[:][0-9a-f]{32}[:]cancelled|'
              || 'approval[:][1-9][0-9]*[:](approved|rejected|expired)|'
              || 'callback[:][1-9][0-9]*[:]attempt[:][0-9]+|'
              || 'alert[:][1-9][0-9]*[:](wecom|smtp)|'
              || 'template[.]sync[:][1-9][0-9]*[:][1-9][0-9]*|'
              || '(template[.]bind|sign[.]bind|sign[.]adopt)[:][1-9][0-9]*[:]'
              || '[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              || '[89ab][0-9a-f]{3}-[0-9a-f]{12}|'
              || 'usage[.]release[:][0-9a-f]{8}-[0-9a-f]{4}-'
              || '[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-'
              || '[0-9a-f]{12})$'
            )
          )
        )
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON
          sms_uncertain_child, usage_chunk_allocation, usage_chunk_release,
          send_inflight_balance, send_inflight_reservation,
          send_admission_state, send_runtime_heartbeat
        TO sms_accept, sms_send
        """
    )
    op.execute(
        """
        GRANT USAGE, SELECT ON SEQUENCE send_inflight_reservation_id_seq
        TO sms_accept, sms_send
        """
    )
    op.execute(
        """
        GRANT SELECT (state, state_epoch, hold_until, valid_until)
        ON send_admission_state TO sms_metrics
        """
    )
    op.execute(
        """
        GRANT SELECT (component, last_heartbeat_at, last_success_at)
        ON send_runtime_heartbeat TO sms_metrics
        """
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (component, last_heartbeat_at, last_success_at) "
        "ON send_runtime_heartbeat FROM sms_metrics"
    )
    op.execute(
        "REVOKE SELECT (state, state_epoch, hold_until, valid_until) "
        "ON send_admission_state FROM sms_metrics"
    )
    op.execute(
        "REVOKE USAGE, SELECT ON SEQUENCE send_inflight_reservation_id_seq "
        "FROM sms_accept, sms_send"
    )
    op.execute(
        """
        REVOKE SELECT, INSERT, UPDATE ON
          sms_uncertain_child, usage_chunk_allocation, usage_chunk_release,
          send_inflight_balance, send_inflight_reservation,
          send_admission_state, send_runtime_heartbeat
        FROM sms_accept, sms_send
        """
    )
    op.execute("DROP TABLE IF EXISTS send_runtime_heartbeat")
    op.execute("DROP TABLE IF EXISTS send_admission_state")
    op.execute("DROP TABLE IF EXISTS send_inflight_reservation")
    op.execute("DROP TABLE IF EXISTS send_inflight_balance")
    op.execute("DROP TABLE IF EXISTS usage_chunk_release")
    op.execute("DROP TABLE IF EXISTS usage_chunk_allocation")
    op.execute("DROP TABLE IF EXISTS sms_uncertain_child")
