"""失败重发增加未覆盖补发。已覆盖成员只按唯一号码索引回填。"""

from __future__ import annotations

from alembic import op

revision = "0135_resend_supplement"
down_revision = "0134_callback_batch_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建补发表，并把能唯一对应的历史子批次记为已覆盖。"""

    op.execute(
        """
        ALTER TABLE sms_resend_action
          ADD COLUMN IF NOT EXISTS coverage_needs_review BOOLEAN NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_resend_supplement (
            id              UUID PRIMARY KEY,
            source_batch_id BIGINT NOT NULL REFERENCES sms_batch(id) ON DELETE RESTRICT,
            child_batch_id  BIGINT UNIQUE REFERENCES sms_batch(id) ON DELETE RESTRICT,
            state           VARCHAR(16) NOT NULL DEFAULT 'preview'
                            CHECK (state IN ('preview','submitted','rejected')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            submitted_at    TIMESTAMPTZ,
            CONSTRAINT ck_resend_supplement_submit CHECK (
              (state='submitted' AND child_batch_id IS NOT NULL
                AND submitted_at IS NOT NULL)
              OR (state<>'submitted' AND child_batch_id IS NULL
                AND submitted_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_resend_supplement_source
          ON sms_resend_supplement(source_batch_id, state)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_resend_supplement_member (
            source_batch_id BIGINT NOT NULL,
            source_message_id BIGINT NOT NULL,
            source_message_created_at TIMESTAMPTZ NOT NULL,
            supplement_id UUID REFERENCES sms_resend_supplement(id) ON DELETE RESTRICT,
            child_batch_id BIGINT REFERENCES sms_batch(id) ON DELETE RESTRICT,
            generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
            claim_state VARCHAR(16) NOT NULL
                        CHECK (claim_state IN (
                          'frozen','submitted','released','needs_review'
                        )),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (
              source_batch_id, source_message_id, source_message_created_at
            ),
            FOREIGN KEY (source_message_id, source_message_created_at)
              REFERENCES sms_message(id, created_at) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_resend_member_supplement
          ON sms_resend_supplement_member(supplement_id)
        """
    )
    op.execute("REVOKE ALL ON sms_resend_supplement FROM PUBLIC")
    op.execute("REVOKE ALL ON sms_resend_supplement_member FROM PUBLIC")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE
          ON sms_resend_supplement, sms_resend_supplement_member
          TO sms_accept
        """
    )
    op.execute(
        """
        INSERT INTO sms_resend_supplement_member(
          source_batch_id, source_message_id, source_message_created_at,
          child_batch_id, generation, claim_state
        )
        SELECT source_batch_id, source_id, source_created, child_batch_id,
          1, 'submitted'
        FROM (
          SELECT a.source_batch_id, a.child_batch_id,
            s.id AS source_id, s.created_at AS source_created,
            count(*) OVER (PARTITION BY c.id, c.created_at) AS source_matches,
            count(*) OVER (PARTITION BY s.id, s.created_at) AS child_matches
          FROM sms_resend_action a
          JOIN sms_message c ON c.batch_id=a.child_batch_id
          JOIN sms_message s ON s.batch_id=a.source_batch_id
            AND s.phone_hmac=c.phone_hmac AND s.key_version=c.key_version
        ) matched
        WHERE source_matches=1 AND child_matches=1
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE sms_resend_action a
        SET coverage_needs_review=true
        WHERE EXISTS (
          SELECT 1 FROM sms_message c
          WHERE c.batch_id=a.child_batch_id
            AND NOT EXISTS (
              SELECT 1 FROM sms_resend_supplement_member m
              JOIN sms_message s
                ON s.id=m.source_message_id
               AND s.created_at=m.source_message_created_at
              WHERE m.child_batch_id=a.child_batch_id
                AND m.claim_state='submitted'
                AND s.phone_hmac=c.phone_hmac
                AND s.key_version=c.key_version
            )
        )
        """
    )


def downgrade() -> None:
    """删除补发事实。普通重发动作保留，覆盖标记一并去掉。"""

    op.execute("DROP TABLE IF EXISTS sms_resend_supplement_member")
    op.execute("DROP TABLE IF EXISTS sms_resend_supplement")
    op.execute(
        "ALTER TABLE sms_resend_action DROP COLUMN IF EXISTS coverage_needs_review"
    )
