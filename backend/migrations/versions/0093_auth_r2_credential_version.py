"""日常改密 CAS 所需的单调凭据版本。"""

from __future__ import annotations

from alembic import op

revision = "0093_auth_r2_credential_version"
down_revision = "0092_send_lifecycle_r2_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE local_credential
          ADD COLUMN IF NOT EXISTS credential_version BIGINT NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE local_credential
          DROP CONSTRAINT IF EXISTS ck_local_credential_version_positive
        """
    )
    op.execute(
        """
        ALTER TABLE local_credential
          ADD CONSTRAINT ck_local_credential_version_positive
          CHECK (credential_version > 0)
        """
    )
    op.execute(
        """
        ALTER TABLE password_change_token
          ADD COLUMN IF NOT EXISTS issued_credential_version BIGINT NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE password_change_token
          DROP CONSTRAINT IF EXISTS ck_password_change_issued_credential_version
        """
    )
    op.execute(
        """
        ALTER TABLE password_change_token
          ADD CONSTRAINT ck_password_change_issued_credential_version
          CHECK (issued_credential_version > 0)
        """
    )


def downgrade() -> None:
    raise NotImplementedError("auth credential_version cannot be downgraded")
