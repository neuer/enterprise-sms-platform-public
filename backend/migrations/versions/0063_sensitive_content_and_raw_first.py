"""Encrypt display content and persist pulled responses before interpretation."""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.services.crypto import CryptoService, EncryptionContext
from app.settings import get_settings

revision = "0063_sensitive_content_and_raw_first"
down_revision = "0062_security_scan_remediations"
branch_labels = None
depends_on = None

BACKFILL_BATCH_SIZE = 500


def _backfill_batch_content(crypto: CryptoService) -> None:
    connection = op.get_bind()
    while True:
        rows = connection.execute(
            text(
                "SELECT id,trim(batch_no) batch_no,content FROM sms_batch "
                "WHERE display_content_enc IS NULL ORDER BY id LIMIT :limit"
            ),
            {"limit": BACKFILL_BATCH_SIZE},
        ).mappings().all()
        if not rows:
            return
        for row in rows:
            batch_no = str(row["batch_no"])
            encrypted = crypto.encrypt_bound_packed_text(
                str(row["content"]),
                EncryptionContext(
                    domain="sms-display-content",
                    table="sms_batch",
                    column="display_content_enc",
                    object_id=batch_no,
                ),
            )
            connection.execute(
                text(
                    "UPDATE sms_batch SET content='[encrypted]',"
                    "display_content_enc=:content_enc WHERE id=:id"
                ),
                {"id": int(row["id"]), "content_enc": encrypted},
            )


def _backfill_reply_content(crypto: CryptoService) -> None:
    connection = op.get_bind()
    while True:
        rows = connection.execute(
            text(
                """
                SELECT trim(event_key) event_key,raw_id,vendor_task_id,custom_id,
                  phone_enc,trim(phone_hmac) phone_hmac,phone_mask,key_version,
                  ext_code,content,reply_time,created_at
                FROM reply_event WHERE event_key_version IS NULL
                ORDER BY event_key LIMIT :limit
                """
            ),
            {"limit": BACKFILL_BATCH_SIZE},
        ).mappings().all()
        if not rows:
            return
        for row in rows:
            old_event_key = str(row["event_key"])
            key_version, event_key = crypto.stable_hmac_fingerprint(
                bytes.fromhex(old_event_key),
                domain="reply-event",
            )
            content_enc = crypto.encrypt_bound_packed_text(
                str(row["content"]),
                EncryptionContext(
                    domain="reply-content",
                    table="reply_event",
                    column="content_enc",
                    object_id=event_key,
                ),
            )
            connection.execute(
                text(
                    """
                    INSERT INTO reply_event(
                      event_key,event_key_version,raw_id,vendor_task_id,custom_id,
                      phone_enc,phone_hmac,phone_mask,key_version,ext_code,
                      content,content_enc,reply_time,created_at
                    ) VALUES (
                      CAST(:event_key AS char(64)),:event_key_version,:raw_id,
                      :vendor_task_id,:custom_id,:phone_enc,
                      CAST(:phone_hmac AS char(64)),:phone_mask,:key_version,:ext_code,
                      '[encrypted]',:content_enc,:reply_time,:created_at
                    )
                    """
                ),
                {
                    "event_key": event_key,
                    "event_key_version": key_version,
                    "raw_id": row["raw_id"],
                    "vendor_task_id": row["vendor_task_id"],
                    "custom_id": row["custom_id"],
                    "phone_enc": row["phone_enc"],
                    "phone_hmac": row["phone_hmac"],
                    "phone_mask": row["phone_mask"],
                    "key_version": row["key_version"],
                    "ext_code": row["ext_code"],
                    "content_enc": content_enc,
                    "reply_time": row["reply_time"],
                    "created_at": row["created_at"],
                },
            )
            connection.execute(
                text(
                    "UPDATE sms_reply SET event_key=CAST(:event_key AS char(64)),"
                    "content='[encrypted]' WHERE event_key=CAST(:old_event_key AS char(64))"
                ),
                {"event_key": event_key, "old_event_key": old_event_key},
            )
            connection.execute(
                text("DELETE FROM reply_event WHERE event_key=CAST(:event_key AS char(64))"),
                {"event_key": old_event_key},
            )


def upgrade() -> None:
    op.execute("ALTER TABLE sms_batch ADD COLUMN IF NOT EXISTS display_content_enc BYTEA")
    op.execute("ALTER TABLE reply_event ADD COLUMN IF NOT EXISTS event_key_version SMALLINT")
    op.execute("ALTER TABLE reply_event ADD COLUMN IF NOT EXISTS content_enc BYTEA")
    op.execute(
        "ALTER TABLE raw_vendor_log ADD COLUMN IF NOT EXISTS http_status SMALLINT DEFAULT 200"
    )
    op.execute(
        "ALTER TABLE raw_vendor_log ADD COLUMN IF NOT EXISTS "
        "content_encoding VARCHAR(16) DEFAULT 'identity'"
    )
    op.execute("UPDATE raw_vendor_log SET http_status=200 WHERE http_status IS NULL")
    op.execute(
        "UPDATE raw_vendor_log SET content_encoding='identity' "
        "WHERE content_encoding IS NULL"
    )

    crypto = CryptoService.from_settings(get_settings())
    _backfill_batch_content(crypto)
    _backfill_reply_content(crypto)

    op.execute("ALTER TABLE sms_batch ALTER COLUMN content SET DEFAULT '[encrypted]'")
    op.execute("ALTER TABLE sms_batch ALTER COLUMN display_content_enc SET NOT NULL")
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_sms_batch_content_marker'
              AND conrelid='sms_batch'::regclass
          ) THEN
            ALTER TABLE sms_batch ADD CONSTRAINT ck_sms_batch_content_marker
            CHECK (content='[encrypted]');
          END IF;
        END $constraint$
        """
    )
    op.execute("ALTER TABLE sms_reply ALTER COLUMN content SET DEFAULT '[encrypted]'")
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_sms_reply_content_marker'
              AND conrelid='sms_reply'::regclass
          ) THEN
            ALTER TABLE sms_reply ADD CONSTRAINT ck_sms_reply_content_marker
            CHECK (content='[encrypted]');
          END IF;
        END $constraint$
        """
    )
    op.execute("ALTER TABLE reply_event ALTER COLUMN content SET DEFAULT '[encrypted]'")
    op.execute("ALTER TABLE reply_event ALTER COLUMN event_key_version SET NOT NULL")
    op.execute("ALTER TABLE reply_event ALTER COLUMN content_enc SET NOT NULL")
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_reply_event_content_marker'
              AND conrelid='reply_event'::regclass
          ) THEN
            ALTER TABLE reply_event ADD CONSTRAINT ck_reply_event_content_marker
            CHECK (content='[encrypted]');
          END IF;
        END $constraint$
        """
    )
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_reply_event_key_version'
              AND conrelid='reply_event'::regclass
          ) THEN
            ALTER TABLE reply_event ADD CONSTRAINT ck_reply_event_key_version
            CHECK (event_key_version>0);
          END IF;
        END $constraint$
        """
    )
    op.execute("ALTER TABLE raw_vendor_log ALTER COLUMN http_status SET DEFAULT 200")
    op.execute("ALTER TABLE raw_vendor_log ALTER COLUMN http_status SET NOT NULL")
    op.execute(
        "ALTER TABLE raw_vendor_log ALTER COLUMN content_encoding SET DEFAULT 'identity'"
    )
    op.execute("ALTER TABLE raw_vendor_log ALTER COLUMN content_encoding SET NOT NULL")
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_raw_vendor_http_status'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log ADD CONSTRAINT ck_raw_vendor_http_status
            CHECK (http_status BETWEEN 100 AND 599);
          END IF;
        END $constraint$
        """
    )
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_raw_vendor_content_encoding'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log ADD CONSTRAINT ck_raw_vendor_content_encoding
            CHECK (content_encoding IN ('identity','unsupported'));
          END IF;
        END $constraint$
        """
    )


def downgrade() -> None:
    # 安全修复只允许前向演进；回退不得重新开放明文正文 writer。
    pass
