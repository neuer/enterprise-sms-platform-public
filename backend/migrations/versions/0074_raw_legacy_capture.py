"""修复已执行旧 0072 时被默认写成 complete 的历史 raw。"""

from __future__ import annotations

from alembic import op

revision = "0074_raw_legacy_capture"
down_revision = "0073_raw_protocol_invalid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0073 的 CHECK 含 protocol_invalid，不含 unknown_legacy。已升级库必须先放宽
    # 再按证据重分类。只改仍为 complete 且带超限/截断/不可判定证据的行；
    # protocol_invalid 与已是 truncated/complete_too_large 的行保持不动。
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_capture_state"
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD CONSTRAINT ck_raw_vendor_capture_state
          CHECK (capture_state IN (
            'complete','complete_too_large','truncated','protocol_invalid','unknown_legacy'
          ))
        """
    )
    op.execute(
        """
        UPDATE raw_vendor_log SET capture_state=CASE
          WHEN error ILIKE '%truncated vendor response%'
            OR error ILIKE '%beyond recovery limit%'
            OR error ILIKE '%exceeds recovery capture limit%'
            OR error ILIKE '%exceeded raw spill quota%'
            OR error ILIKE '%exceeds hard limit%'
            OR error ILIKE '%body exceeds hard limit%'
            THEN 'truncated'
          WHEN (
              error ILIKE '%oversized payload persisted%'
              OR error ILIKE '%exceeds automatic processing limit%'
              OR error ILIKE '%too large to parse%'
            ) AND octet_length(payload_enc) >= CASE
              WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
              ELSE 28
            END AND (
              octet_length(payload_enc) - CASE
                WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
                ELSE 28
              END
            ) > 67108864
            THEN 'unknown_legacy'
          WHEN error ILIKE '%oversized payload persisted%'
            OR error ILIKE '%exceeds automatic processing limit%'
            OR error ILIKE '%too large to parse%'
            THEN 'complete_too_large'
          WHEN octet_length(payload_enc) < CASE
              WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
              ELSE 28
            END
            THEN 'unknown_legacy'
          WHEN (
              octet_length(payload_enc) - CASE
                WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
                ELSE 28
              END
            ) > 67108864
            THEN 'truncated'
          WHEN (
              octet_length(payload_enc) - CASE
                WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
                ELSE 28
              END
            ) = 67108864
            THEN 'unknown_legacy'
          WHEN (
              octet_length(payload_enc) - CASE
                WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
                ELSE 28
              END
            ) > 4194304
            THEN 'complete_too_large'
          WHEN (
              octet_length(payload_enc) - CASE
                WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
                ELSE 28
              END
            ) = 4194304
            AND processed=false
            THEN 'unknown_legacy'
          ELSE 'complete'
        END
        WHERE capture_state='complete'
          AND (
            error ILIKE '%truncated vendor response%'
            OR error ILIKE '%beyond recovery limit%'
            OR error ILIKE '%exceeds recovery capture limit%'
            OR error ILIKE '%exceeded raw spill quota%'
            OR error ILIKE '%exceeds hard limit%'
            OR error ILIKE '%body exceeds hard limit%'
            OR error ILIKE '%oversized payload persisted%'
            OR error ILIKE '%exceeds automatic processing limit%'
            OR error ILIKE '%too large to parse%'
            OR octet_length(payload_enc) < CASE
              WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
              ELSE 28
            END
            OR (
              octet_length(payload_enc) - CASE
                WHEN substring(payload_enc from 1 for 4)=convert_to('SME2','UTF8') THEN 32
                ELSE 28
              END
            ) >= 4194304
          )
        """
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ALTER COLUMN capture_state SET DEFAULT 'complete'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE raw_vendor_log SET capture_state='truncated'
        WHERE capture_state='unknown_legacy'
        """
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_capture_state"
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD CONSTRAINT ck_raw_vendor_capture_state
          CHECK (capture_state IN (
            'complete','complete_too_large','truncated','protocol_invalid'
          ))
        """
    )
