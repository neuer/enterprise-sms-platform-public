"""AD 会话策略以 PostgreSQL 单行 revision 为权威围栏。"""

from __future__ import annotations

from alembic import op

revision = "0097_auth_session_policy"
down_revision = "0096_idempotency_claim_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_session_policy (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            revision BIGINT NOT NULL CHECK (revision >= 1),
            ad_session_max_age_minutes INTEGER NOT NULL
                CHECK (ad_session_max_age_minutes BETWEEN 15 AND 10080),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by VARCHAR(64)
        )
        """
    )
    op.execute(
        """
        INSERT INTO auth_session_policy (id, revision, ad_session_max_age_minutes)
        SELECT 1, 1, CAST(value AS INTEGER)
        FROM sys_config
        WHERE key = 'ad_session_max_age_minutes'
          AND NOT EXISTS (SELECT 1 FROM auth_session_policy WHERE id = 1)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON auth_session_policy TO sms_accept")
    op.execute(
        """
        GRANT SELECT (id, revision, ad_session_max_age_minutes, updated_at)
        ON auth_session_policy TO sms_metrics
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_session_policy")
