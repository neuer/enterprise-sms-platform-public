"""API Key 摘要增加显式算法标识，禁止把 NULL 版本静默当成 SHA-256。"""

from __future__ import annotations

from alembic import op

revision = "0091_api_key_digest_algorithms"
down_revision = "0090_vendor_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS api_key_hash_algorithm VARCHAR(32)
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS api_key_prev_hash_algorithm VARCHAR(32)
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS api_key_hash_migrated_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS api_key_prev_hash_migrated_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        UPDATE app
           SET api_key_hash_algorithm='api_pepper'
         WHERE api_key_hash_version IS NOT NULL
           AND api_key_hash_algorithm IS NULL
        """
    )
    op.execute(
        """
        UPDATE app
           SET api_key_prev_hash_algorithm='api_pepper'
         WHERE api_key_prev_hash_version IS NOT NULL
           AND api_key_prev_hash_algorithm IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO sys_config (key, value, value_type, description)
        VALUES (
          'api_key_unclassified_algorithms',
          '',
          'str',
          '未分类历史 API Key 摘要允许的候选算法，逗号分隔；空则不得验证未分类行'
        )
        ON CONFLICT (key) DO NOTHING
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_api_key_hash_algorithm'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_api_key_hash_algorithm
              CHECK (
                api_key_hash_algorithm IS NULL
                OR api_key_hash_algorithm IN (
                  'legacy_sha256',
                  'legacy_data_hmac_pepper_v1',
                  'api_pepper'
                )
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_api_key_prev_hash_algorithm'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_api_key_prev_hash_algorithm
              CHECK (
                api_key_prev_hash_algorithm IS NULL
                OR api_key_prev_hash_algorithm IN (
                  'legacy_sha256',
                  'legacy_data_hmac_pepper_v1',
                  'api_pepper'
                )
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_api_key_algorithm_version'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_api_key_algorithm_version
              CHECK (
                (
                  api_key_hash_algorithm IS NULL
                  AND api_key_hash_version IS NULL
                )
                OR (
                  api_key_hash_algorithm='api_pepper'
                  AND api_key_hash_version IS NOT NULL
                )
                OR (
                  api_key_hash_algorithm IN (
                    'legacy_sha256',
                    'legacy_data_hmac_pepper_v1'
                  )
                  AND api_key_hash_version IS NULL
                )
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_api_key_prev_algorithm_version'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_api_key_prev_algorithm_version
              CHECK (
                (
                  api_key_prev_hash_algorithm IS NULL
                  AND api_key_prev_hash_version IS NULL
                )
                OR (
                  api_key_prev_hash_algorithm='api_pepper'
                  AND api_key_prev_hash_version IS NOT NULL
                )
                OR (
                  api_key_prev_hash_algorithm IN (
                    'legacy_sha256',
                    'legacy_data_hmac_pepper_v1'
                  )
                  AND api_key_prev_hash_version IS NULL
                )
              );
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        GRANT UPDATE (
          api_key_hash, api_key_hash_version, api_key_hash_algorithm,
          api_key_hash_migrated_at, api_key_prev_hash,
          api_key_prev_hash_version, api_key_prev_hash_algorithm,
          api_key_prev_hash_migrated_at
        ) ON app TO sms_auth
        """
    )


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (api_key_hash, api_key_hash_version, api_key_hash_algorithm, "
        "api_key_hash_migrated_at, api_key_prev_hash, api_key_prev_hash_version, "
        "api_key_prev_hash_algorithm, api_key_prev_hash_migrated_at) "
        "ON app FROM sms_auth"
    )
    op.execute("ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_api_key_algorithm_version")
    op.execute(
        "ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_api_key_prev_algorithm_version"
    )
    op.execute("ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_api_key_hash_algorithm")
    op.execute(
        "ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_api_key_prev_hash_algorithm"
    )
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS api_key_hash_migrated_at")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS api_key_prev_hash_migrated_at")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS api_key_hash_algorithm")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS api_key_prev_hash_algorithm")
