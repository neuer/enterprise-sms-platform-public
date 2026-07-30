"""为报告回调引用增加稳定、无 PII 的事件身份。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_callback_event_keys"
down_revision: str | None = "0011_chunk_retry_not_before"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    inspector = sa.inspect(op.get_bind())
    if "callback_report_event" not in set(inspector.get_table_names()):
        op.create_table(
            "callback_report_event",
            sa.Column("event_key", sa.CHAR(64), primary_key=True),
            sa.Column("batch_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("message_created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("message_status", sa.String(10), nullable=False),
            sa.Column("report_desc", sa.String(128), nullable=False, server_default=""),
            sa.Column("report_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["batch_id"], ["sms_batch.id"]),
        )
        op.create_index(
            "idx_cb_report_event_batch",
            "callback_report_event",
            ["batch_id", "created_at"],
        )
        op.create_index("idx_cb_report_event_created", "callback_report_event", ["created_at"])
        op.execute(
            "GRANT SELECT,INSERT,DELETE ON callback_report_event TO sms_app"
        )
    column_names = {
        str(item["name"]) for item in inspector.get_columns("callback_task")
    }
    if "event_keys" not in column_names:
        op.add_column(
            "callback_task",
            sa.Column(
                "event_keys",
                sa.ARRAY(sa.CHAR(64)),
                nullable=False,
                server_default=sa.text("'{}'::char(64)[]"),
            ),
        )
    index_names = {
        str(item["name"]) for item in inspector.get_indexes("callback_task")
    }
    if "idx_cb_event_keys" not in index_names:
        op.create_index(
            "idx_cb_event_keys",
            "callback_task",
            ["event_keys"],
            postgresql_using="gin",
        )
    op.execute(
        """
        DO $$ DECLARE bad_task bigint; bad_count bigint; BEGIN
          SELECT min(task_id),count(*) INTO bad_task,bad_count FROM (
            SELECT min(t.id) task_id
            FROM callback_task t
            CROSS JOIN LATERAL unnest(t.message_ids,t.message_times)
              AS r(message_id,message_created_at)
            WHERE t.event='message.report'
            GROUP BY r.message_id,r.message_created_at
            HAVING count(*)>1
          ) ambiguous;
          IF bad_count>0 THEN
            RAISE EXCEPTION 'ambiguous legacy callback events task_id=% count=%',
              bad_task,bad_count;
          END IF;
          SELECT min(t.id),count(*) INTO bad_task,bad_count
            FROM callback_task t
            CROSS JOIN LATERAL unnest(t.message_ids,t.message_times)
              AS r(message_id,message_created_at)
            LEFT JOIN sms_message m
              ON m.id=r.message_id AND m.created_at=r.message_created_at
            LEFT JOIN sms_chunk c ON c.id=m.chunk_id
            WHERE t.event='message.report' AND (
              m.id IS NULL OR c.id IS NULL OR
              m.report_status IS NULL OR m.report_time IS NULL
            );
          IF bad_count>0 THEN
            RAISE EXCEPTION 'callback event key backfill incomplete task_id=% count=%',
              bad_task,bad_count;
          END IF;
        END $$
        """
    )
    op.execute(
        """
        WITH refs AS (
          SELECT t.id task_id,r.message_id,r.message_created_at,r.ord
          FROM callback_task t
          CROSS JOIN LATERAL unnest(t.message_ids,t.message_times)
            WITH ORDINALITY AS r(message_id,message_created_at,ord)
          WHERE t.event='message.report'
        ), values_to_hash AS (
          SELECT r.task_id,r.ord,
            CAST(r.message_id AS text) message_id,
            to_char(r.message_created_at AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') message_created_at,
            trim(c.custom_id) custom_id,
            CAST(m.report_status AS text) report_status,
            COALESCE(m.report_desc,'') report_desc,
            to_char(m.report_time AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') report_time,
            m.status message_status,COALESCE(m.report_desc,'') snapshot_desc,
            m.report_time snapshot_time,m.batch_id,
            r.message_id raw_message_id,r.message_created_at raw_message_created_at
          FROM refs r
          JOIN sms_message m
            ON m.id=r.message_id AND m.created_at=r.message_created_at
          JOIN sms_chunk c ON c.id=m.chunk_id
          WHERE m.report_status IS NOT NULL AND m.report_time IS NOT NULL
        ), keyed AS (
          SELECT task_id,ord,encode(digest(
            octet_length(message_id)::text || ':' || message_id ||
            octet_length(message_created_at)::text || ':' || message_created_at ||
            octet_length(custom_id)::text || ':' || custom_id ||
            octet_length(report_status)::text || ':' || report_status ||
            octet_length(report_desc)::text || ':' || report_desc ||
            octet_length(report_time)::text || ':' || report_time,
            'sha256'),'hex')::char(64) event_key,
            message_status,snapshot_desc,snapshot_time,batch_id,
            raw_message_id,raw_message_created_at
          FROM values_to_hash
        ), inserted_events AS (
          INSERT INTO callback_report_event(
            event_key,batch_id,message_id,message_created_at,
            message_status,report_desc,report_time
          ) SELECT DISTINCT event_key,batch_id,raw_message_id,raw_message_created_at,
            message_status,snapshot_desc,snapshot_time FROM keyed
          ON CONFLICT(event_key) DO NOTHING RETURNING event_key
        ), grouped AS (
          SELECT task_id,array_agg(event_key ORDER BY ord)::char(64)[] event_keys
          FROM keyed, (SELECT count(*) FROM inserted_events) ensured GROUP BY task_id
        )
        UPDATE callback_task t SET event_keys=g.event_keys
        FROM grouped g WHERE t.id=g.task_id
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM callback_task
            WHERE event='message.report' AND (
              cardinality(message_ids)<>cardinality(message_times) OR
              cardinality(message_ids)<>cardinality(event_keys)
            )
          ) THEN
            RAISE EXCEPTION 'callback event key backfill incomplete';
          END IF;
        END $$
        """
    )
    op.drop_constraint("chk_cb_message_refs", "callback_task", type_="check")
    op.create_check_constraint(
        "chk_cb_message_refs",
        "callback_task",
        "cardinality(message_ids) = cardinality(message_times) "
        "AND cardinality(message_ids) = cardinality(event_keys)",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ DECLARE bad_task bigint; bad_count bigint; BEGIN
          SELECT min(task_id),count(*) INTO bad_task,bad_count FROM (
            SELECT min(t.id) task_id
            FROM callback_task t
            CROSS JOIN LATERAL unnest(t.message_ids,t.message_times)
              AS r(message_id,message_created_at)
            WHERE t.event='message.report'
            GROUP BY r.message_id,r.message_created_at
            HAVING count(*)>1
          ) unsafe;
          IF bad_count>0 THEN
            RAISE EXCEPTION
              'callback event downgrade unsafe task_id=% count=%',bad_task,bad_count;
          END IF;
        END $$
        """
    )
    op.drop_constraint("chk_cb_message_refs", "callback_task", type_="check")
    op.create_check_constraint(
        "chk_cb_message_refs",
        "callback_task",
        "cardinality(message_ids) = cardinality(message_times)",
    )
    op.drop_column("callback_task", "event_keys")
    op.drop_table("callback_report_event")
