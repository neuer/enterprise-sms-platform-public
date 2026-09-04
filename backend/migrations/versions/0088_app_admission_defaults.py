"""应用最小权限默认值、成本限流与可审计豁免。"""

from __future__ import annotations

from alembic import op

revision = "0088_app_admission_defaults"
down_revision = "0087_uncertain_conservative_terminal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app
          ALTER COLUMN allowed_categories SET DEFAULT 'notice'
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS recipient_limit_per_min INTEGER
            NOT NULL DEFAULT 10000
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS segment_limit_per_min INTEGER
            NOT NULL DEFAULT 10000
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS max_in_flight_chunks INTEGER
            NOT NULL DEFAULT 200
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS allow_market_api_bulk BOOLEAN
            NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS ip_allowlist_exempt_until TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS unlimited_quota_exempt_until TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS admission_exempt_note VARCHAR(200)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_recipient_limit_per_min'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_recipient_limit_per_min
              CHECK (recipient_limit_per_min BETWEEN 1 AND 100000000);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_segment_limit_per_min'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_segment_limit_per_min
              CHECK (segment_limit_per_min BETWEEN 1 AND 100000000);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='app'::regclass
              AND conname='ck_app_max_in_flight_chunks'
          ) THEN
            ALTER TABLE app
              ADD CONSTRAINT ck_app_max_in_flight_chunks
              CHECK (max_in_flight_chunks BETWEEN 1 AND 100000);
          END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_recipient_limit_per_min"
    )
    op.execute(
        "ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_segment_limit_per_min"
    )
    op.execute(
        "ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_max_in_flight_chunks"
    )
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS recipient_limit_per_min")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS segment_limit_per_min")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS max_in_flight_chunks")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS allow_market_api_bulk")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS ip_allowlist_exempt_until")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS unlimited_quota_exempt_until")
    op.execute("ALTER TABLE app DROP COLUMN IF EXISTS admission_exempt_note")
    op.execute(
        """
        ALTER TABLE app
          ALTER COLUMN allowed_categories SET DEFAULT 'verify,notice,market'
        """
    )
