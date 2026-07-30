"""增加真实厂商受控联调的每日计费条证据账本。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_vendor_live_test_budget"
down_revision: str | None = "0015_account_provider_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只扩展分片计数与 PII-free 账本，并在数据库层硬限制每日 100 条。"""

    op.execute(
        """
        ALTER TABLE sms_chunk
        ADD COLUMN IF NOT EXISTS vendor_attempt_count SMALLINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='chk_chunk_vendor_attempt_count'
              AND conrelid='sms_chunk'::regclass
          ) THEN
            ALTER TABLE sms_chunk
            ADD CONSTRAINT chk_chunk_vendor_attempt_count
            CHECK (vendor_attempt_count >= 0);
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_test_daily_usage (
            usage_date DATE PRIMARY KEY,
            in_flight_segments INTEGER NOT NULL DEFAULT 0
              CHECK (in_flight_segments >= 0),
            confirmed_segments INTEGER NOT NULL DEFAULT 0
              CHECK (confirmed_segments >= 0),
            uncertain_segments INTEGER NOT NULL DEFAULT 0
              CHECK (uncertain_segments >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_vendor_test_daily_total CHECK (
              in_flight_segments + confirmed_segments + uncertain_segments <= 100
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_test_send_attempt (
            id BIGSERIAL PRIMARY KEY,
            usage_date DATE NOT NULL
              REFERENCES vendor_test_daily_usage(usage_date) ON DELETE RESTRICT,
            chunk_id BIGINT NOT NULL REFERENCES sms_chunk(id) ON DELETE RESTRICT,
            attempt_no SMALLINT NOT NULL CHECK (attempt_no > 0),
            segments INTEGER NOT NULL CHECK (segments > 0),
            status VARCHAR(16) NOT NULL
              CHECK (status IN ('reserved','confirmed','uncertain','released')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ,
            UNIQUE (chunk_id, attempt_no),
            CONSTRAINT chk_vendor_test_attempt_settlement CHECK (
              (status = 'reserved' AND settled_at IS NULL)
              OR (status <> 'reserved' AND settled_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_vendor_test_reserved_chunk
        ON vendor_test_send_attempt(chunk_id)
        WHERE status = 'reserved'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vendor_test_attempt_usage_status
        ON vendor_test_send_attempt(usage_date, status)
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON vendor_test_daily_usage TO sms_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON vendor_test_send_attempt TO sms_app"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE vendor_test_send_attempt_id_seq TO sms_app"
    )
    op.execute(
        "REVOKE DELETE, TRUNCATE ON vendor_test_daily_usage FROM sms_app"
    )
    op.execute(
        "REVOKE DELETE, TRUNCATE ON vendor_test_send_attempt FROM sms_app"
    )


def downgrade() -> None:
    """真实发送证据不可由自动迁移删除。"""

    raise RuntimeError("vendor live test budget downgrade is permanently disabled")
