"""Apply round-four metadata protection and callback authority fencing."""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.services.crypto import CryptoService, EncryptionContext
from app.settings import get_settings
from app.vendor.identifiers import vendor_identifier_pseudonym

revision = "0065_security_scan_round4"
down_revision = "0064_security_scan_round3"
branch_labels = None
depends_on = None

BACKFILL_BATCH_SIZE = 500


def _pseudonymize_vendor_metadata(crypto: CryptoService) -> None:
    connection = op.get_bind()
    specs = (
        ("sms_chunk", ("id",), (("vendor_task_id", "vendor-task-id"),)),
        (
            "report_event",
            ("event_key",),
            (("vendor_task_id", "vendor-task-id"), ("custom_id", "vendor-custom-id")),
        ),
        (
            "reply_event",
            ("event_key",),
            (("vendor_task_id", "vendor-task-id"), ("custom_id", "vendor-custom-id")),
        ),
        ("sms_reply", ("id", "created_at"), (("vendor_task_id", "vendor-task-id"),)),
        (
            "unmatched_report",
            ("id",),
            (("vendor_task_id", "vendor-task-id"), ("custom_id", "vendor-custom-id")),
        ),
    )
    for table, keys, columns in specs:
        cursor: tuple[object, ...] | None = None
        present = " OR ".join(f"{column} IS NOT NULL" for column, _domain in columns)
        while True:
            selected = ",".join((*keys, *(column for column, _domain in columns)))
            params: dict[str, object] = {"limit": BACKFILL_BATCH_SIZE}
            predicates = [f"({present})"]
            if cursor is not None:
                left = f"({','.join(keys)})" if len(keys) > 1 else keys[0]
                right_names = tuple(f"cursor_{key}" for key in keys)
                right = (
                    f"({','.join(f':{name}' for name in right_names)})"
                    if len(keys) > 1
                    else f":{right_names[0]}"
                )
                predicates.append(f"{left}>{right}")
                params.update(dict(zip(right_names, cursor, strict=True)))
            where = "WHERE " + " AND ".join(predicates) + " "
            rows = connection.execute(
                text(
                    f"SELECT {selected} FROM {table} {where}"
                    f"ORDER BY {','.join(keys)} LIMIT :limit"
                ),
                params,
            ).mappings().all()
            if not rows:
                break
            for row in rows:
                params = {key: row[key] for key in keys}
                assignments: list[str] = []
                for column, domain in columns:
                    value = row[column]
                    if value is None:
                        continue
                    params[column] = vendor_identifier_pseudonym(
                        crypto,
                        str(value),
                        domain=domain,
                    )
                    assignments.append(f"{column}=:{column}")
                if not assignments:
                    continue
                predicate = " AND ".join(f"{key}=:{key}" for key in keys)
                connection.execute(
                    text(f"UPDATE {table} SET {','.join(assignments)} WHERE {predicate}"),
                    params,
                )
            cursor = tuple(rows[-1][key] for key in keys)


def _add_check(table: str, name: str, expression: str) -> None:
    """兼容最终 schema 基线与增量升级两种建库路径。"""

    op.execute(
        f"""
        DO $constraint$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='{name}' AND conrelid='{table}'::regclass
          ) THEN
            ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression});
          END IF;
        END $constraint$
        """
    )


def _backfill_template_names(crypto: CryptoService) -> None:
    connection = op.get_bind()
    while True:
        rows = connection.execute(
            text(
                "SELECT id,name FROM sms_template "
                "WHERE name_enc IS NULL ORDER BY id LIMIT :limit"
            ),
            {"limit": BACKFILL_BATCH_SIZE},
        ).mappings().all()
        if not rows:
            return
        for row in rows:
            template_id = int(row["id"])
            name_enc = crypto.encrypt_bound_packed_text(
                str(row["name"]),
                EncryptionContext(
                    domain="sms-template-name",
                    table="sms_template",
                    column="name_enc",
                    object_id=str(template_id),
                ),
            )
            connection.execute(
                text(
                    "UPDATE sms_template SET name='[encrypted]',name_enc=:name_enc "
                    "WHERE id=:id AND name_enc IS NULL"
                ),
                {"id": template_id, "name_enc": name_enc},
            )


def upgrade() -> None:
    crypto = CryptoService.from_settings(get_settings())
    _pseudonymize_vendor_metadata(crypto)
    op.execute(
        """
        UPDATE raw_vendor_log r SET custom_ids=ARRAY(
          SELECT DISTINCT candidate FROM unnest(r.custom_ids) candidate
          JOIN sms_chunk c ON trim(c.custom_id)=candidate ORDER BY candidate
        )
        """
    )
    op.execute("UPDATE reply_event SET ext_code='' WHERE ext_code IS DISTINCT FROM ''")
    op.execute("UPDATE sms_reply SET ext_code='' WHERE ext_code IS DISTINCT FROM ''")
    op.execute("ALTER TABLE reply_event ALTER COLUMN ext_code SET DEFAULT ''")
    op.execute("ALTER TABLE reply_event ALTER COLUMN ext_code SET NOT NULL")
    op.execute("ALTER TABLE sms_reply ALTER COLUMN ext_code SET DEFAULT ''")
    op.execute("ALTER TABLE sms_reply ALTER COLUMN ext_code SET NOT NULL")
    _add_check(
        "sms_chunk",
        "ck_sms_chunk_vendor_task_pseudonym",
        "vendor_task_id IS NULL OR vendor_task_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check(
        "sms_reply",
        "ck_sms_reply_vendor_task_pseudonym",
        "vendor_task_id IS NULL OR vendor_task_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check("sms_reply", "ck_sms_reply_ext_code_redacted", "ext_code=''")
    _add_check(
        "report_event",
        "ck_report_event_vendor_task_pseudonym",
        "vendor_task_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check(
        "report_event",
        "ck_report_event_custom_pseudonym",
        "custom_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check(
        "reply_event",
        "ck_reply_event_vendor_task_pseudonym",
        "vendor_task_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check(
        "reply_event",
        "ck_reply_event_custom_pseudonym",
        "custom_id IS NULL OR custom_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check("reply_event", "ck_reply_event_ext_code_redacted", "ext_code=''")
    _add_check(
        "unmatched_report",
        "ck_unmatched_vendor_task_pseudonym",
        "vendor_task_id IS NULL OR vendor_task_id ~ '^[0-9a-f]{64}$'",
    )
    _add_check(
        "unmatched_report",
        "ck_unmatched_custom_pseudonym",
        "custom_id IS NULL OR custom_id ~ '^[0-9a-f]{64}$'",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_raw_vendor_custom_ids()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $function$
        DECLARE candidate TEXT;
        BEGIN
          FOREACH candidate IN ARRAY NEW.custom_ids LOOP
            IF candidate !~ '^[A-Za-z0-9]{32}$'
               OR NOT EXISTS (
                 SELECT 1 FROM public.sms_chunk c WHERE trim(c.custom_id)=candidate
               ) THEN
              RAISE EXCEPTION 'raw vendor customId is not a known platform identifier'
                USING ERRCODE='23514';
            END IF;
          END LOOP;
          RETURN NEW;
        END;
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION enforce_raw_vendor_custom_ids() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_raw_vendor_custom_ids ON raw_vendor_log")
    op.execute(
        """
        CREATE TRIGGER trg_raw_vendor_custom_ids
        BEFORE INSERT OR UPDATE OF custom_ids ON raw_vendor_log
        FOR EACH ROW EXECUTE FUNCTION enforce_raw_vendor_custom_ids()
        """
    )
    op.execute("ALTER TABLE sms_template ADD COLUMN IF NOT EXISTS name_enc BYTEA")
    _backfill_template_names(crypto)
    op.execute("ALTER TABLE sms_template ALTER COLUMN name SET DEFAULT '[encrypted]'")
    op.execute("ALTER TABLE sms_template ALTER COLUMN name_enc SET NOT NULL")
    _add_check(
        "sms_template",
        "ck_sms_template_name_marker",
        "name='[encrypted]'",
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS callback_authority_lease (
          app_id BIGINT PRIMARY KEY REFERENCES app(id) ON DELETE CASCADE,
          task_id BIGINT NOT NULL UNIQUE REFERENCES callback_task(id) ON DELETE CASCADE,
          lease_id UUID NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("REVOKE ALL ON callback_authority_lease FROM PUBLIC")
    op.execute("GRANT SELECT,DELETE ON callback_authority_lease TO sms_accept")
    op.execute("GRANT SELECT,INSERT,DELETE ON callback_authority_lease TO sms_callback")
    op.execute(
        """
        UPDATE import_task SET filename=CASE
          WHEN lower(filename) LIKE '%.xlsx' THEN 'upload.xlsx'
          ELSE 'upload.csv'
        END
        """
    )
    op.execute("ALTER TABLE import_task ALTER COLUMN filename SET NOT NULL")
    _add_check(
        "import_task",
        "ck_import_task_canonical_filename",
        "filename IN ('upload.csv','upload.xlsx')",
    )


def downgrade() -> None:
    # 安全修复只允许前向演进；不得重新开放模板名称或上传文件名明文。
    pass
