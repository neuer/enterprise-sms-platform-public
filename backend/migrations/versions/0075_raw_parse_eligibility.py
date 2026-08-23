"""raw_vendor_log.parse_state 与 replay_eligibility：解析面与自动重放资格。"""

from __future__ import annotations

from alembic import op

revision = "0075_raw_parse_eligibility"
down_revision = "0074_raw_legacy_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线建库已含两列；存量升级在此补齐，两条路径幂等收敛。
    # 默认 manual：无法判定的历史行不得默认进入 automatic。
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS parse_state VARCHAR(24) NOT NULL DEFAULT 'unattempted'
        """
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS replay_eligibility VARCHAR(16) NOT NULL DEFAULT 'manual'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_raw_vendor_parse_state'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT ck_raw_vendor_parse_state
              CHECK (parse_state IN (
                'unattempted','transient_failure','protocol_invalid','processed'
              ));
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_raw_vendor_replay_eligibility'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT ck_raw_vendor_replay_eligibility
              CHECK (replay_eligibility IN ('automatic','manual','never'));
          END IF;
        END $$
        """
    )
    op.execute(
        """
        UPDATE raw_vendor_log SET
          parse_state=CASE
          WHEN processed THEN 'processed'
          WHEN capture_state='protocol_invalid' THEN 'protocol_invalid'
          WHEN capture_state IN ('truncated','unknown_legacy') THEN 'unattempted'
          WHEN content_encoding<>'identity' THEN 'protocol_invalid'
          WHEN http_status < 200 OR http_status >= 300 THEN 'protocol_invalid'
          WHEN error ILIKE '%raw payload integrity mismatch%' THEN 'protocol_invalid'
          WHEN error ILIKE '%OperationalError%'
            OR error ILIKE '%InterfaceError%'
            OR error ILIKE '%Deadlock%'
            OR error ILIKE '%LockNotAvailable%'
            OR error ILIKE '%SerializationError%'
            OR error ILIKE '%CancelledError%'
            OR error ILIKE '%TimeoutError%'
            OR error ILIKE '%connection reset%'
            OR error ILIKE '%connection refused%'
            OR error ILIKE '%too many connections%'
            OR error ILIKE '%server closed the connection%'
            THEN 'transient_failure'
          WHEN error ILIKE '%VendorProtocolError%'
            OR error ILIKE '%VendorApiError%'
            OR error ILIKE '%vendor response parsing failed%'
            OR error ILIKE '%vendor response is not JSON%'
            OR error ILIKE '%vendor response envelope%'
            OR error ILIKE '%content-encoding%'
            OR error ILIKE '%vendor HTTP status%'
            OR error ILIKE '%raw vendor envelope is invalid%'
            OR error ILIKE '%must be an object array%'
            OR error ILIKE 'skipped % invalid % items'
            THEN 'protocol_invalid'
          ELSE 'unattempted'
        END,
          replay_eligibility=CASE
          WHEN processed THEN 'never'
          WHEN capture_state IN ('truncated','protocol_invalid','unknown_legacy') THEN 'never'
          WHEN content_encoding<>'identity' THEN 'never'
          WHEN http_status < 200 OR http_status >= 300 THEN 'never'
          WHEN capture_state='complete_too_large' THEN 'manual'
          WHEN error ILIKE '%raw payload integrity mismatch%' THEN 'never'
          WHEN error ILIKE '%VendorApiError%'
            OR error ILIKE '%content-encoding is forbidden%'
            OR error ILIKE '%vendor HTTP status%'
            OR error ILIKE '%vendor response content-encoding%'
            THEN 'never'
          WHEN error ILIKE '%OperationalError%'
            OR error ILIKE '%InterfaceError%'
            OR error ILIKE '%Deadlock%'
            OR error ILIKE '%LockNotAvailable%'
            OR error ILIKE '%SerializationError%'
            OR error ILIKE '%CancelledError%'
            OR error ILIKE '%TimeoutError%'
            OR error ILIKE '%connection reset%'
            OR error ILIKE '%connection refused%'
            OR error ILIKE '%too many connections%'
            OR error ILIKE '%server closed the connection%'
            THEN 'automatic'
          WHEN error ILIKE '%VendorProtocolError%'
            OR error ILIKE '%vendor response parsing failed%'
            OR error ILIKE '%vendor response is not JSON%'
            OR error ILIKE '%vendor response envelope%'
            OR error ILIKE '%raw vendor envelope is invalid%'
            OR error ILIKE '%must be an object array%'
            OR error ILIKE 'skipped % invalid % items'
            THEN 'manual'
          WHEN error IS NULL OR btrim(error)='' THEN
            CASE WHEN capture_state='complete' THEN 'automatic' ELSE 'manual' END
          ELSE 'manual'
        END
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_auto_replay
          ON raw_vendor_log(replay_attempts, id)
          WHERE processed=FALSE AND replay_eligibility='automatic'
        """
    )
    op.execute(
        "GRANT SELECT (replay_eligibility) ON raw_vendor_log TO sms_metrics"
    )


def downgrade() -> None:
    op.execute("REVOKE SELECT (replay_eligibility) ON raw_vendor_log FROM sms_metrics")
    op.execute("DROP INDEX IF EXISTS idx_raw_auto_replay")
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_replay_eligibility"
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_parse_state"
    )
    op.execute("ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS replay_eligibility")
    op.execute("ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS parse_state")
