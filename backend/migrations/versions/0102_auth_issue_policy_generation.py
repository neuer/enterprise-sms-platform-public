"""AD 签发绑定权威策略世代，并作废修复前可能过宽的 AD 会话。"""

from __future__ import annotations

from alembic import op

revision = "0102_auth_issue_policy_generation"
down_revision = "0101_inflight_balance_conservation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth_session_policy
          ADD COLUMN IF NOT EXISTS min_accepted_policy_revision BIGINT
        """
    )
    op.execute(
        """
        UPDATE auth_session_policy
        SET min_accepted_policy_revision = 1
        WHERE min_accepted_policy_revision IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE auth_session_policy
          ALTER COLUMN min_accepted_policy_revision SET DEFAULT 1,
          ALTER COLUMN min_accepted_policy_revision SET NOT NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_auth_session_policy_min_accepted'
          ) THEN
            ALTER TABLE auth_session_policy
              ADD CONSTRAINT ck_auth_session_policy_min_accepted
              CHECK (
                min_accepted_policy_revision >= 1
                AND min_accepted_policy_revision <= revision
              );
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        GRANT SELECT (min_accepted_policy_revision)
        ON auth_session_policy TO sms_metrics
        """
    )
    op.execute(
        """
        UPDATE auth_session_policy
        SET min_accepted_policy_revision = revision + 1,
            revision = revision + 1,
            updated_at = now(),
            updated_by = '0102_auth_issue_policy'
        WHERE id = 1
        """
    )
    op.execute(
        """
        UPDATE user_account ua
        SET security_version = ua.security_version + 1,
            updated_at = now()
        WHERE EXISTS (
          SELECT 1
          FROM auth_identity ai
          JOIN auth_provider ap ON ap.id = ai.provider_id
          WHERE ai.account_id = ua.id
            AND ap.code = 'ad'
        )
        """
    )


def downgrade() -> None:
    return
