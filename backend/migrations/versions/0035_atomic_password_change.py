"""首次改密令牌与密码更新使用同一 PostgreSQL 事务。"""

from __future__ import annotations

from alembic import op

revision = "0035_atomic_password_change"
down_revision = "0034_database_role_matrix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS password_change_token (
          id BIGSERIAL PRIMARY KEY,
          token_hash CHAR(64) NOT NULL UNIQUE
            CHECK (token_hash ~ '^[0-9a-f]{64}$'),
          account_id BIGINT NOT NULL
            REFERENCES user_account(id) ON DELETE RESTRICT,
          identity_id BIGINT NOT NULL,
          provider_code VARCHAR(64) NOT NULL,
          purpose VARCHAR(32) NOT NULL
            CHECK (purpose IN ('initial_password')),
          normalized_login_name VARCHAR(64) NOT NULL,
          issued_security_version BIGINT NOT NULL
            CHECK (issued_security_version>0),
          status VARCHAR(12) NOT NULL DEFAULT 'available'
            CHECK (status IN ('available','consumed','revoked','expired')),
          expires_at TIMESTAMPTZ NOT NULL,
          consumed_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_password_change_identity
            FOREIGN KEY (identity_id,account_id)
            REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
          CONSTRAINT ck_password_change_consumed CHECK (
            (status='consumed' AND consumed_at IS NOT NULL)
            OR (status<>'consumed' AND consumed_at IS NULL)
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_password_change_account_available
          ON password_change_token(account_id,expires_at)
          WHERE status='available'
        """
    )
    op.execute(
        """
        GRANT SELECT,INSERT,UPDATE ON password_change_token TO sms_auth
        """
    )
    op.execute(
        """
        GRANT USAGE,SELECT ON SEQUENCE password_change_token_id_seq TO sms_auth
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('public.password_change_token') IS NOT NULL
             AND EXISTS (SELECT 1 FROM password_change_token) THEN
            RAISE EXCEPTION
              'cannot downgrade atomic password change with retained tokens';
          END IF;
        END
        $$
        """
    )
    op.execute("DROP TABLE IF EXISTS password_change_token")
