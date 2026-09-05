"""幂等 Claim 以 PostgreSQL generation 为权威围栏。"""

from __future__ import annotations

from alembic import op

revision = "0096_idempotency_claim_generation"
down_revision = "0095_vendor_attempt_generation_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_claim (
            id           BIGSERIAL PRIMARY KEY,
            scope_kind   VARCHAR(16)  NOT NULL,
            scope_id     VARCHAR(64)  NOT NULL,
            biz_id       VARCHAR(32)  NOT NULL,
            token        CHAR(32)     NOT NULL,
            fingerprint  VARCHAR(64)  NOT NULL DEFAULT '',
            generation   INTEGER      NOT NULL CHECK (generation >= 1),
            expires_at   TIMESTAMPTZ  NOT NULL,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uk_idempotency_claim_scope UNIQUE (scope_kind, scope_id, biz_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_idempotency_claim_expire
          ON idempotency_claim(expires_at)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_claim TO sms_accept")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE idempotency_claim_id_seq TO sms_accept"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS idempotency_claim")
