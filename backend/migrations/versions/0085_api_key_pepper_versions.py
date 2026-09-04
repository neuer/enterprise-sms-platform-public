"""API Key 摘要绑定独立 pepper 版本，不再复用 data_hmac_key。"""

from __future__ import annotations

from alembic import op

revision = "0085_api_key_pepper_versions"
down_revision = "0084_auth_security_and_ad_freshness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS api_key_hash_version SMALLINT
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS api_key_prev_hash_version SMALLINT
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_api_key_hash_version'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_api_key_hash_version
              CHECK (
                api_key_hash_version IS NULL
                OR api_key_hash_version BETWEEN 1 AND 32767
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_api_key_prev_hash_version'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_api_key_prev_hash_version
              CHECK (
                api_key_prev_hash_version IS NULL
                OR api_key_prev_hash_version BETWEEN 1 AND 32767
              );
          END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
