"""允许分片发送走独立 child Outbox，不再在批次长事务内解密。"""

from __future__ import annotations

from alembic import op

revision = "0086_chunk_ready_outbox"
down_revision = "0085_api_key_pepper_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_task_name"
    )
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_args_no_pii"
    )
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_refs_no_pii"
    )
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
          'app.tasks.outbox.trigger_job'
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
              task_name='app.tasks.send.process_chunk'
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
                'app.tasks.sync_template'
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


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
