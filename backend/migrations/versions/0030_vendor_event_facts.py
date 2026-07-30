"""建立不可变厂商事件事实、数据库去重与单调报告投影。"""

from __future__ import annotations

from alembic import op

revision = "0030_vendor_event_facts"
down_revision = "0029_worker_fencing_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """expand/backfill/contract：历史投影保留为兼容事实，新事件由 PK 去重。"""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report_event (
          event_key CHAR(64) PRIMARY KEY
            CHECK (event_key ~ '^[0-9a-f]{64}$'),
          raw_id BIGINT,
          vendor_task_id VARCHAR(64) NOT NULL,
          custom_id VARCHAR(64) NOT NULL,
          phone_enc BYTEA NOT NULL,
          phone_hmac CHAR(64) NOT NULL
            CHECK (phone_hmac ~ '^[0-9a-f]{64}$'),
          phone_mask VARCHAR(11) NOT NULL,
          key_version SMALLINT NOT NULL CHECK (key_version>0),
          report_status SMALLINT NOT NULL,
          message_status VARCHAR(10) NOT NULL
            CHECK (message_status IN ('delivered','failed','unknown','other')),
          report_desc VARCHAR(128) NOT NULL DEFAULT '',
          report_time TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_report_event_raw ON report_event(raw_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_event_custom "
        "ON report_event(custom_id,report_time)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_event (
          event_key CHAR(64) PRIMARY KEY
            CHECK (event_key ~ '^[0-9a-f]{64}$'),
          raw_id BIGINT,
          vendor_task_id VARCHAR(64) NOT NULL,
          custom_id VARCHAR(64),
          phone_enc BYTEA NOT NULL,
          phone_hmac CHAR(64) NOT NULL
            CHECK (phone_hmac ~ '^[0-9a-f]{64}$'),
          phone_mask VARCHAR(11) NOT NULL,
          key_version SMALLINT NOT NULL CHECK (key_version>0),
          ext_code VARCHAR(8),
          content VARCHAR(500) NOT NULL,
          reply_time TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_reply_event_raw ON reply_event(raw_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reply_event_time ON reply_event(reply_time)")
    op.execute(
        "ALTER TABLE sms_message ADD COLUMN IF NOT EXISTS report_event_key CHAR(64)"
    )
    op.execute("ALTER TABLE sms_reply ADD COLUMN IF NOT EXISTS event_key CHAR(64)")
    op.execute(
        "ALTER TABLE unmatched_report ADD COLUMN IF NOT EXISTS event_key CHAR(64)"
    )
    op.execute(
        """
        ALTER TABLE callback_task
          ADD COLUMN IF NOT EXISTS source_report_event_key CHAR(64)
        """
    )
    op.execute(
        """
        INSERT INTO report_event(
          event_key,raw_id,vendor_task_id,custom_id,
          phone_enc,phone_hmac,phone_mask,key_version,
          report_status,message_status,report_desc,report_time
        )
        SELECT
          encode(digest(
            'legacy-report:'||m.id::text||':'||
            to_char(m.created_at AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'sha256'
          ),'hex')::char(64),
          NULL,COALESCE(c.vendor_task_id,'legacy-report'),
          COALESCE(c.custom_id,'legacy-report:'||m.id::text),
          m.phone_enc,m.phone_hmac,m.phone_mask,m.key_version,m.report_status,
          CASE m.status
            WHEN 'delivered' THEN 'delivered'
            WHEN 'failed' THEN 'failed'
            WHEN 'unknown' THEN 'unknown'
            ELSE 'other'
          END,
          COALESCE(m.report_desc,''),m.report_time
        FROM sms_message m
        LEFT JOIN sms_chunk c ON c.id=m.chunk_id
        WHERE m.report_status IS NOT NULL AND m.report_time IS NOT NULL
        ON CONFLICT(event_key) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE sms_message m SET report_event_key=encode(digest(
          'legacy-report:'||m.id::text||':'||
          to_char(m.created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
          'sha256'
        ),'hex')::char(64)
        WHERE m.report_status IS NOT NULL AND m.report_time IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report_event_projection (
          event_key CHAR(64) PRIMARY KEY
            REFERENCES report_event(event_key) ON DELETE RESTRICT,
          batch_id BIGINT NOT NULL,
          message_id BIGINT NOT NULL,
          message_created_at TIMESTAMPTZ NOT NULL,
          projection_changed BOOLEAN NOT NULL,
          projected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          FOREIGN KEY(message_id,message_created_at)
            REFERENCES sms_message(id,created_at) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_report_projection_message
          ON report_event_projection(message_id,message_created_at,projected_at)
        """
    )
    op.execute(
        """
        INSERT INTO report_event_projection(
          event_key,batch_id,message_id,message_created_at,projection_changed
        )
        SELECT report_event_key,batch_id,id,created_at,true
        FROM sms_message WHERE report_event_key IS NOT NULL;
        """
    )
    op.execute(
        """
        INSERT INTO report_event(
          event_key,raw_id,vendor_task_id,custom_id,
          phone_enc,phone_hmac,phone_mask,key_version,
          report_status,message_status,report_desc,report_time
        )
        SELECT
          encode(digest('legacy-unmatched:'||u.id::text,'sha256'),'hex')
            ::char(64),
          NULL,COALESCE(u.vendor_task_id,'legacy-unmatched'),
          COALESCE(u.custom_id,'legacy-unmatched:'||u.id::text),
          u.phone_enc,u.phone_hmac,u.phone_mask,u.key_version,
          COALESCE(u.report_status,0),
          CASE COALESCE(u.report_status,0)
            WHEN 1 THEN 'delivered'
            WHEN 2 THEN 'failed'
            WHEN 99 THEN 'failed'
            WHEN 0 THEN 'unknown'
            ELSE 'other'
          END,
          COALESCE(u.report_desc,''),COALESCE(u.report_time,u.created_at)
        FROM unmatched_report u
        ON CONFLICT(event_key) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE unmatched_report u SET event_key=encode(digest(
          'legacy-unmatched:'||u.id::text,'sha256'
        ),'hex')::char(64)
        WHERE u.event_key IS NULL
        """
    )
    op.execute("ALTER TABLE unmatched_report ALTER COLUMN event_key SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='uk_unmatched_report_event'
          ) THEN
            ALTER TABLE unmatched_report ADD CONSTRAINT uk_unmatched_report_event
              UNIQUE(event_key);
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_cb_source_report_event'
          ) THEN
            ALTER TABLE callback_task ADD CONSTRAINT fk_cb_source_report_event
              FOREIGN KEY(source_report_event_key)
              REFERENCES report_event(event_key) ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_cb_batch_source_event
          ON callback_task(batch_id,source_report_event_key)
          WHERE event='batch.finished' AND source_report_event_key IS NOT NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_unmatched_report_event'
          ) THEN
            ALTER TABLE unmatched_report ADD CONSTRAINT fk_unmatched_report_event
              FOREIGN KEY(event_key) REFERENCES report_event(event_key)
              ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        INSERT INTO reply_event(
          event_key,raw_id,vendor_task_id,custom_id,
          phone_enc,phone_hmac,phone_mask,key_version,
          ext_code,content,reply_time
        )
        SELECT
          encode(digest(
            'legacy-reply:'||r.id::text||':'||
            to_char(r.created_at AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'sha256'
          ),'hex')::char(64),
          NULL,COALESCE(r.vendor_task_id,'legacy-reply:'||r.id::text),NULL,
          r.phone_enc,r.phone_hmac,r.phone_mask,r.key_version,
          r.ext_code,r.content,COALESCE(r.reply_time,r.created_at)
        FROM sms_reply r
        ON CONFLICT(event_key) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE sms_reply r SET event_key=encode(digest(
          'legacy-reply:'||r.id::text||':'||
          to_char(r.created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
          'sha256'
        ),'hex')::char(64)
        WHERE r.event_key IS NULL
        """
    )
    op.execute("ALTER TABLE sms_reply ALTER COLUMN event_key SET NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reply_event ON sms_reply(event_key)")
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='fk_reply_event'
          ) THEN
            ALTER TABLE sms_reply ADD CONSTRAINT fk_reply_event
              FOREIGN KEY(event_key) REFERENCES reply_event(event_key)
              ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='fk_message_report_event'
          ) THEN
            ALTER TABLE sms_message ADD CONSTRAINT fk_message_report_event
              FOREIGN KEY(report_event_key) REFERENCES report_event(event_key)
              ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON report_event, reply_event FROM sms_app"
    )
    op.execute("GRANT SELECT, INSERT ON report_event, reply_event TO sms_app")
    op.execute(
        "REVOKE DELETE, TRUNCATE ON report_event_projection FROM sms_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON report_event_projection TO sms_app"
    )


def downgrade() -> None:
    """出现新采集事实后拒绝降级，避免丢失唯一去重证据。"""

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM report_event WHERE raw_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM reply_event WHERE raw_id IS NOT NULL) THEN
            RAISE EXCEPTION 'cannot downgrade vendor event facts with ingested evidence';
          END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE sms_message DROP CONSTRAINT fk_message_report_event"
    )
    op.execute("DROP INDEX uk_cb_batch_source_event")
    op.execute(
        "ALTER TABLE callback_task DROP CONSTRAINT fk_cb_source_report_event"
    )
    op.execute("ALTER TABLE callback_task DROP COLUMN source_report_event_key")
    op.execute("ALTER TABLE sms_reply DROP CONSTRAINT fk_reply_event")
    op.execute("DROP INDEX idx_reply_event")
    op.execute("ALTER TABLE sms_reply DROP COLUMN event_key")
    op.execute(
        "ALTER TABLE unmatched_report DROP CONSTRAINT fk_unmatched_report_event"
    )
    op.execute(
        "ALTER TABLE unmatched_report DROP CONSTRAINT uk_unmatched_report_event"
    )
    op.execute("ALTER TABLE unmatched_report DROP COLUMN event_key")
    op.execute("DROP TABLE report_event_projection")
    op.execute("DROP TABLE reply_event")
    op.execute("DROP TABLE report_event")
    op.execute("ALTER TABLE sms_message DROP COLUMN report_event_key")
