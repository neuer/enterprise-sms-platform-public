"""增加事务性 Outbox 事实表与租约/fencing 状态机。"""

from __future__ import annotations

from alembic import op

revision = "0027_transactional_outbox"
down_revision = "0026_stable_principal_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅做 expand：旧 API/worker 可继续运行，新 dispatcher 可渐进启用。"""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox_event (
          id UUID PRIMARY KEY,
          dedup_key VARCHAR(192) NOT NULL UNIQUE,
          event_type VARCHAR(64) NOT NULL,
          aggregate_type VARCHAR(32) NOT NULL,
          aggregate_id VARCHAR(128) NOT NULL,
          task_name VARCHAR(128) NOT NULL
            CONSTRAINT ck_outbox_task_name
            CHECK (task_name IN (
              'app.tasks.send.process_batch',
              'app.tasks.deliver_callback',
              'app.tasks.outbox.compensate_quota',
              'app.tasks.outbox.deliver_alert'
            )),
          queue VARCHAR(16) NOT NULL
            CHECK (queue IN ('realtime','bulk','callback')),
          args JSONB NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(args)='array')
            CONSTRAINT ck_outbox_args_scalar_refs CHECK (
              NOT (
                args @? '$[*] ? (
                  @.type() != "string" && @.type() != "number"
                )'
              )
            ),
          state VARCHAR(16) NOT NULL DEFAULT 'pending'
            CHECK (state IN (
              'pending','leased','published','processing','completed','dead'
            )),
          attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
          failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
          max_attempts SMALLINT NOT NULL DEFAULT 12 CHECK (max_attempts BETWEEN 1 AND 100),
          next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          lease_id UUID,
          lease_expires_at TIMESTAMPTZ,
          published_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          last_error VARCHAR(64),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT ck_outbox_lease_pair CHECK (
            (lease_id IS NULL AND lease_expires_at IS NULL)
            OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
          ),
          CONSTRAINT ck_outbox_args_no_pii CHECK (
            (
              args::text !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              OR (
                task_name='app.tasks.send.process_batch'
                AND jsonb_array_length(args)=1
                AND jsonb_typeof(args->0)='string'
                AND args->>0 ~ '^[0-9a-f]{32}$'
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
            )
            AND args::text !~ (
              '"(phone|phones|mobile|mobiles|phone_enc|phone_hmac|'
              || 'content|body|secret|password)"[[:space:]]*:'
            )
          ),
          CONSTRAINT ck_outbox_refs_no_pii CHECK (
            (
              aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              OR (
                task_name='app.tasks.send.process_batch'
                AND aggregate_id ~ '^[0-9a-f]{32}$'
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
            )
            AND (
              dedup_key !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              OR dedup_key ~ (
                '^(batch[.]ready[:][0-9a-f]{32}|'
                || 'scheduled[:][0-9a-f]{32}[:]ready|'
                || 'batch[:][0-9a-f]{32}[:]cancelled|'
                || 'approval[:][1-9][0-9]*[:](approved|rejected|expired)|'
                || 'callback[:][1-9][0-9]*[:]attempt[:][0-9]+|'
                || 'alert[:][1-9][0-9]*[:](wecom|smtp))$'
              )
            )
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_dispatch_due
          ON outbox_event(next_attempt_at,created_at)
          WHERE state IN ('pending','leased','published')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_processing_lease
          ON outbox_event(lease_expires_at)
          WHERE state='processing'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_backlog
          ON outbox_event(state,created_at)
          WHERE state<>'completed'
        """
    )
    op.execute(
        "GRANT SELECT,INSERT,UPDATE ON outbox_event TO sms_app"
    )
    op.execute("REVOKE DELETE,TRUNCATE ON outbox_event FROM sms_app")


def downgrade() -> None:
    """仅在没有未完成事件时允许回滚，避免静默丢失已受理副作用。"""

    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM outbox_event WHERE state<>'completed'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade transactional outbox with unfinished events';
          END IF;
        END
        $downgrade$
        """
    )
    op.drop_table("outbox_event")
