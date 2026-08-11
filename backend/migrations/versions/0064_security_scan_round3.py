"""Encrypt template bodies and close scan-round-three metadata boundaries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from alembic import op
from sqlalchemy import text

from app.services.crypto import CryptoService, EncryptionContext
from app.settings import get_settings

revision = "0064_security_scan_round3"
down_revision = "0063_sensitive_content_and_raw_first"
branch_labels = None
depends_on = None

BACKFILL_BATCH_SIZE = 500
PHONE_PATTERN = "(^|[^0-9])1[0-9]{10}([^0-9]|$)"
METADATA_FIELDS = (
    ("sms_batch", "id", "remark"),
    ("blacklist", "phone_hmac", "remark"),
    ("approval", "id", "reason"),
    ("sms_template", "id", "vendor_reject_reason"),
    ("sms_sign", "id", "vendor_reject_reason"),
)


def _backfill_template_content(crypto: CryptoService) -> None:
    connection = op.get_bind()
    while True:
        rows = connection.execute(
            text(
                "SELECT id,content FROM sms_template "
                "WHERE content_enc IS NULL ORDER BY id LIMIT :limit"
            ),
            {"limit": BACKFILL_BATCH_SIZE},
        ).mappings().all()
        if not rows:
            return
        for row in rows:
            template_id = int(row["id"])
            content_enc = crypto.encrypt_bound_packed_text(
                str(row["content"]),
                EncryptionContext(
                    domain="sms-template-content",
                    table="sms_template",
                    column="content_enc",
                    object_id=str(template_id),
                ),
            )
            connection.execute(
                text(
                    "UPDATE sms_template SET content='[encrypted]',content_enc=:content_enc "
                    "WHERE id=:id AND content_enc IS NULL"
                ),
                {"id": template_id, "content_enc": content_enc},
            )


def _metadata_rows(
    table: str,
    key_column: str,
    value_column: str,
) -> Iterable[Any]:
    connection = op.get_bind()
    return connection.execute(
        text(
            f"SELECT {key_column} source_key,CAST({key_column} AS text) source_row,"
            f"{value_column} source_value "
            f"FROM {table} WHERE {value_column} ~ :phone_pattern ORDER BY {key_column}"
        ),
        {"phone_pattern": PHONE_PATTERN},
    ).mappings()


def _archive_and_redact_metadata(crypto: CryptoService) -> None:
    connection = op.get_bind()
    for table, key_column, value_column in METADATA_FIELDS:
        for row in _metadata_rows(table, key_column, value_column):
            source_key = row["source_key"]
            source_row = str(row["source_row"]).strip()
            object_id = f"{table}:{source_row}:{value_column}"
            value_enc = crypto.encrypt_bound_packed_text(
                str(row["source_value"]),
                EncryptionContext(
                    domain="sensitive-metadata-archive",
                    table="sensitive_metadata_archive",
                    column="value_enc",
                    object_id=object_id,
                ),
            )
            connection.execute(
                text(
                    """
                    INSERT INTO sensitive_metadata_archive(
                      source_table,source_row,source_column,value_enc
                    ) VALUES(:source_table,:source_row,:source_column,:value_enc)
                    ON CONFLICT(source_table,source_row,source_column) DO NOTHING
                    """
                ),
                {
                    "source_table": table,
                    "source_row": source_row,
                    "source_column": value_column,
                    "value_enc": value_enc,
                },
            )
            connection.execute(
                text(
                    f"UPDATE {table} SET {value_column}='[redacted-phone]' "
                    f"WHERE {key_column}=:source_key "
                    f"AND {value_column} ~ :phone_pattern"
                ),
                {"source_key": source_key, "phone_pattern": PHONE_PATTERN},
            )


def _add_no_phone_constraint(table: str, column: str, constraint: str) -> None:
    op.execute(
        f"""
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='{constraint}' AND conrelid='{table}'::regclass
          ) THEN
            ALTER TABLE {table} ADD CONSTRAINT {constraint}
            CHECK ({column} IS NULL OR {column} !~ '{PHONE_PATTERN}');
          END IF;
        END $constraint$
        """
    )


def upgrade() -> None:
    op.execute("ALTER TABLE sms_template ADD COLUMN IF NOT EXISTS content_enc BYTEA")
    crypto = CryptoService.from_settings(get_settings())
    _backfill_template_content(crypto)
    op.execute("ALTER TABLE sms_template ALTER COLUMN content SET DEFAULT '[encrypted]'")
    op.execute("ALTER TABLE sms_template ALTER COLUMN content_enc SET NOT NULL")
    op.execute(
        """
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_sms_template_content_marker'
              AND conrelid='sms_template'::regclass
          ) THEN
            ALTER TABLE sms_template ADD CONSTRAINT ck_sms_template_content_marker
            CHECK (content='[encrypted]');
          END IF;
        END $constraint$
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sensitive_metadata_archive (
          source_table VARCHAR(32) NOT NULL,
          source_row VARCHAR(128) NOT NULL,
          source_column VARCHAR(32) NOT NULL,
          value_enc BYTEA NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY(source_table,source_row,source_column),
          CONSTRAINT ck_sensitive_metadata_archive_source CHECK (
            (source_table='sms_batch' AND source_column='remark')
            OR (source_table='blacklist' AND source_column='remark')
            OR (source_table='approval' AND source_column='reason')
            OR (source_table='sms_template' AND source_column='vendor_reject_reason')
            OR (source_table='sms_sign' AND source_column='vendor_reject_reason')
          )
        )
        """
    )
    op.execute("REVOKE ALL ON sensitive_metadata_archive FROM PUBLIC")
    _archive_and_redact_metadata(crypto)

    for table, column, constraint in (
        ("sms_batch", "remark", "ck_sms_batch_remark_no_phone"),
        ("blacklist", "remark", "ck_blacklist_remark_no_phone"),
        ("approval", "reason", "ck_approval_reason_no_phone"),
        (
            "sms_template",
            "vendor_reject_reason",
            "ck_sms_template_reject_reason_no_phone",
        ),
        ("sms_sign", "vendor_reject_reason", "ck_sms_sign_reject_reason_no_phone"),
    ):
        _add_no_phone_constraint(table, column, constraint)

    op.execute(
        "ALTER TABLE usage_reservation "
        "DROP CONSTRAINT IF EXISTS ck_usage_request_key_no_pii"
    )
    op.execute(
        """
        ALTER TABLE usage_reservation ADD CONSTRAINT ck_usage_request_key_no_pii CHECK (
          request_key ~ (
            '^(acceptance[:](v2[:][0-9a-f]{64}[:][0-9]{8}|'
            ||'[0-9]+[:][0-9a-f]{64}[:][0-9]{8}|'
            ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
            ||'[89ab][0-9a-f]{3}-[0-9a-f]{12})|'
            ||'legacy[:]batch[:][0-9a-f]{32})$'
          )
        )
        """
    )


def downgrade() -> None:
    # 安全修复只允许前向演进；回退不得重新开放明文模板或跨主体账本键。
    pass
