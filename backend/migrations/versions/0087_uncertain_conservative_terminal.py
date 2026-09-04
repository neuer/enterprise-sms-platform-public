"""uncertain 保守终态、最大生命周期与双人处置合同。"""

from __future__ import annotations

from alembic import op

revision = "0087_uncertain_conservative_terminal"
down_revision = "0086_chunk_ready_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_batch
          DROP CONSTRAINT IF EXISTS sms_batch_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD CONSTRAINT sms_batch_status_check CHECK (status IN (
            'pending_approval','rejected','scheduled','queued',
            'sending','completed','completed_unknown','cancelled',
            'balance_blocked','expired'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          DROP CONSTRAINT IF EXISTS sms_chunk_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD CONSTRAINT sms_chunk_status_check CHECK (status IN (
            'pending','submitting','submitted','failed','retrying',
            'uncertain','unknown_terminal'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS unknown_terminal_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE sms_chunk
          ADD COLUMN IF NOT EXISTS late_evidence_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_unknown_terminal
          ON sms_chunk(unknown_terminal_at)
          WHERE status = 'unknown_terminal'
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_uncertain_resolution (
            id                   BIGSERIAL PRIMARY KEY,
            chunk_id             BIGINT      NOT NULL UNIQUE REFERENCES sms_chunk(id),
            batch_id             BIGINT      NOT NULL REFERENCES sms_batch(id),
            action               VARCHAR(24) NOT NULL
                                 CHECK (action IN (
                                   'confirm_accepted','confirm_not_accepted',
                                   'keep_unknown','resend_new_batch'
                                 )),
            state                VARCHAR(16) NOT NULL DEFAULT 'proposed'
                                 CHECK (state IN ('proposed','confirmed')),
            proposer_account_id  BIGINT      NOT NULL REFERENCES user_account(id),
            confirmer_account_id BIGINT      REFERENCES user_account(id),
            child_batch_id       BIGINT      REFERENCES sms_batch(id),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            confirmed_at         TIMESTAMPTZ,
            CONSTRAINT ck_uncertain_resolution_distinct CHECK (
              confirmer_account_id IS NULL
              OR proposer_account_id <> confirmer_account_id
            ),
            CONSTRAINT ck_uncertain_resolution_confirm_pair CHECK (
              (state='proposed' AND confirmer_account_id IS NULL
               AND confirmed_at IS NULL)
              OR (state='confirmed' AND confirmer_account_id IS NOT NULL
                  AND confirmed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_uncertain_resolution_batch
          ON sms_uncertain_resolution(batch_id, created_at DESC)
        """
    )
    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description)
        VALUES(
          'uncertain_max_lifetime_hours','72','int',
          'uncertain无证据后进入保守终态的时长(小时)'
        )
        ON CONFLICT (key) DO NOTHING
        """
    )
    op.execute(
        "GRANT SELECT ON sms_uncertain_resolution TO sms_accept, sms_send"
    )
    op.execute(
        "GRANT INSERT, UPDATE ON sms_uncertain_resolution TO sms_accept, sms_send"
    )
    op.execute(
        """
        GRANT USAGE, SELECT ON SEQUENCE sms_uncertain_resolution_id_seq
          TO sms_accept, sms_send
        """
    )
    op.execute(
        """
        GRANT SELECT (uncertain_since, late_evidence_at)
          ON sms_chunk TO sms_metrics
        """
    )
    op.execute(
        "GRANT SELECT (state) ON sms_uncertain_resolution TO sms_metrics"
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
