"""增加配额/频控事实账本、HMAC 轮换主体和版本化 Redis 投影。"""

from __future__ import annotations

from alembic import op

revision = "0028_usage_fact_ledger"
down_revision = "0027_transactional_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """expand + 当日事实回填；Redis 从此只承载可重建投影。"""

    op.execute("CREATE SEQUENCE IF NOT EXISTS usage_projection_version_seq")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_reservation (
          id UUID PRIMARY KEY,
          request_key VARCHAR(192) NOT NULL,
          app_id BIGINT NOT NULL CHECK (app_id>=0),
          dept VARCHAR(128) NOT NULL,
          category VARCHAR(8) NOT NULL
            CHECK (category IN ('verify','notice','market')),
          usage_date DATE NOT NULL,
          state VARCHAR(20) NOT NULL DEFAULT 'reserved'
            CHECK (state IN (
              'reserved','committed','release_requested','released','uncertain'
            )),
          quota_cost INTEGER NOT NULL DEFAULT 0 CHECK (quota_cost>=0),
          app_limit INTEGER NOT NULL DEFAULT 0 CHECK (app_limit>=0),
          dept_limit INTEGER NOT NULL DEFAULT 0 CHECK (dept_limit>=0),
          release_event_id VARCHAR(192) UNIQUE,
          last_error VARCHAR(64),
          reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          committed_at TIMESTAMPTZ,
          release_requested_at TIMESTAMPTZ,
          released_at TIMESTAMPTZ,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT ck_usage_request_key_no_pii CHECK (
            request_key ~ (
              '^(acceptance[:]([0-9]+[:][0-9a-f]{64}[:][0-9]{8}|'
              ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              ||'[89ab][0-9a-f]{3}-[0-9a-f]{12})|'
              ||'legacy[:]batch[:][0-9a-f]{32})$'
            )
          ),
          CONSTRAINT ck_usage_release_event_no_pii CHECK (
            release_event_id IS NULL
            OR release_event_id ~ (
              '^(batch[:][0-9a-f]{32}[:]cancelled|'
              ||'approval[:][1-9][0-9]*[:](rejected|expired)|usage[:]'
              ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              ||'[89ab][0-9a-f]{3}-[0-9a-f]{12}[:]'
              ||'(acceptance-failed|all-filtered|idempotent-reuse|'
              ||'orphan-recovery))$'
            )
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_usage_reservation_active_request
          ON usage_reservation(request_key)
          WHERE state<>'released'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_reservation_recovery
          ON usage_reservation(updated_at)
          WHERE state IN ('reserved','uncertain','release_requested')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_reservation_retention
          ON usage_reservation(usage_date,state)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_frequency_subject (
          id UUID PRIMARY KEY,
          projection_hmac CHAR(64) NOT NULL UNIQUE
            CHECK (projection_hmac ~ '^[0-9a-f]{64}$'),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_frequency_alias (
          subject_id UUID NOT NULL
            REFERENCES usage_frequency_subject(id) ON DELETE CASCADE,
          key_version SMALLINT NOT NULL CHECK (key_version>0),
          phone_hmac CHAR(64) NOT NULL UNIQUE
            CHECK (phone_hmac ~ '^[0-9a-f]{64}$'),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY(subject_id,key_version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_quota_entry (
          reservation_id UUID NOT NULL
            REFERENCES usage_reservation(id) ON DELETE CASCADE,
          dimension_kind VARCHAR(8) NOT NULL
            CHECK (dimension_kind IN ('app','dept','volume')),
          dimension_value VARCHAR(160) NOT NULL,
          usage_date DATE NOT NULL,
          amount INTEGER NOT NULL CHECK (amount>=0),
          projection_key VARCHAR(256) NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY(reservation_id,dimension_kind),
          CONSTRAINT ck_usage_quota_projection_key CHECK (
            projection_key ~ '^quota:(app|dept|volume:app):'
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_quota_dimension
          ON usage_quota_entry(projection_key,usage_date)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_frequency_entry (
          reservation_id UUID NOT NULL
            REFERENCES usage_reservation(id) ON DELETE CASCADE,
          subject_id UUID NOT NULL
            REFERENCES usage_frequency_subject(id) ON DELETE RESTRICT,
          app_id BIGINT,
          category VARCHAR(8) NOT NULL
            CHECK (category IN ('verify','market')),
          window_kind VARCHAR(8) NOT NULL
            CHECK (window_kind IN ('minute','day')),
          window_key VARCHAR(16) NOT NULL,
          usage_date DATE NOT NULL,
          projection_key VARCHAR(256) NOT NULL,
          counted BOOLEAN NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY(reservation_id,subject_id,window_kind),
          CONSTRAINT ck_usage_frequency_scope CHECK (
            (category='verify' AND app_id IS NULL)
            OR (category='market' AND app_id IS NOT NULL AND window_kind='day')
          ),
          CONSTRAINT ck_usage_frequency_projection_key CHECK (
            projection_key ~ '^freq:(v|m):[0-9a-f:]+'
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_frequency_dimension
          ON usage_frequency_entry(projection_key,usage_date)
          WHERE counted
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_frequency_subject_window
          ON usage_frequency_entry(
            subject_id,category,app_id,window_kind,window_key
          )
          WHERE counted
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_projection (
          dimension_key VARCHAR(256) PRIMARY KEY,
          kind VARCHAR(16) NOT NULL CHECK (kind IN ('quota','frequency')),
          usage_date DATE NOT NULL,
          window_key VARCHAR(24) NOT NULL,
          value BIGINT NOT NULL DEFAULT 0 CHECK (value>=0),
          version BIGINT NOT NULL DEFAULT nextval('usage_projection_version_seq')
            CHECK (version>0),
          expires_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_projection_rebuild
          ON usage_projection(usage_date,expires_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_projection_drift (
          kind VARCHAR(16) PRIMARY KEY CHECK (kind IN ('quota','frequency')),
          mismatched_dimensions INTEGER NOT NULL DEFAULT 0
            CHECK (mismatched_dimensions>=0),
          absolute_delta BIGINT NOT NULL DEFAULT 0 CHECK (absolute_delta>=0),
          checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO usage_projection_drift(kind)
        VALUES('quota'),('frequency')
        ON CONFLICT(kind) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description) VALUES
          (
            'usage_projection_reconcile_seconds','300','int',
            '配额/频控投影漂移巡检间隔(重启beat生效)'
          ),
          (
            'usage_ledger_retention_days','90','int',
            '已过期配额/频控事实账本保留天数'
          )
        ON CONFLICT(key) DO NOTHING
        """
    )
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS usage_reservation_id UUID
            REFERENCES usage_reservation(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_sms_batch_usage_reservation
          ON sms_batch(usage_reservation_id)
          WHERE usage_reservation_id IS NOT NULL
        """
    )

    # 迁移瞬间仍在上海当日配额/频控窗口内的已受理批次必须回填，
    # 防止上线重建时把已有用量误当作零。
    op.execute(
        """
        INSERT INTO usage_reservation(
          id,request_key,app_id,dept,category,usage_date,state,
          quota_cost,app_limit,dept_limit,reserved_at,committed_at,updated_at
        )
        SELECT
          md5('usage:legacy:batch:'||b.id::text)::uuid,
          'legacy:batch:'||trim(b.batch_no),
          COALESCE(b.app_id,0),b.dept,b.category,
          (b.created_at AT TIME ZONE 'Asia/Shanghai')::date,
          'committed',b.quota_cost,COALESCE(a.daily_quota,0),COALESCE(d.daily_quota,0),
          b.created_at,b.created_at,b.updated_at
        FROM sms_batch b
        LEFT JOIN app a ON a.id=b.app_id
        LEFT JOIN dept_quota d ON d.dept=b.dept
        WHERE (b.created_at AT TIME ZONE 'Asia/Shanghai')::date
              =(now() AT TIME ZONE 'Asia/Shanghai')::date
          AND b.status NOT IN ('rejected','expired','cancelled')
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE sms_batch b SET usage_reservation_id=r.id
        FROM usage_reservation r
        WHERE r.request_key='legacy:batch:'||trim(b.batch_no)
          AND b.usage_reservation_id IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO usage_quota_entry(
          reservation_id,dimension_kind,dimension_value,usage_date,
          amount,projection_key,expires_at
        )
        SELECT r.id,dimensions.kind,dimensions.value,r.usage_date,
          r.quota_cost,dimensions.projection_key,
          ((r.usage_date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
        FROM usage_reservation r
        CROSS JOIN LATERAL (
          VALUES
            ('app'::varchar,r.app_id::text,
             'quota:app:'||r.app_id::text||':'||to_char(r.usage_date,'YYYYMMDD')),
            ('dept'::varchar,r.dept,
             'quota:dept:'||r.dept||':'||to_char(r.usage_date,'YYYYMMDD')),
            ('volume'::varchar,r.app_id::text||':'||r.category,
             'quota:volume:app:'||r.app_id::text||':'||r.category||':'||
               to_char(r.usage_date,'YYYYMMDD'))
        ) dimensions(kind,value,projection_key)
        WHERE r.request_key LIKE 'legacy:batch:%'
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO usage_frequency_subject(id,projection_hmac)
        SELECT DISTINCT md5('usage:subject:'||m.phone_hmac)::uuid,m.phone_hmac
        FROM sms_message m
        JOIN sms_batch b ON b.id=m.batch_id
        WHERE b.usage_reservation_id IS NOT NULL
          AND b.category IN ('verify','market')
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO usage_frequency_alias(subject_id,key_version,phone_hmac)
        SELECT DISTINCT s.id,m.key_version,m.phone_hmac
        FROM sms_message m
        JOIN sms_batch b ON b.id=m.batch_id
        JOIN usage_frequency_subject s ON s.projection_hmac=m.phone_hmac
        WHERE b.usage_reservation_id IS NOT NULL
          AND b.category IN ('verify','market')
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO usage_frequency_entry(
          reservation_id,subject_id,app_id,category,window_kind,window_key,
          usage_date,projection_key,counted,expires_at
        )
        SELECT DISTINCT
          b.usage_reservation_id,s.id,NULL::bigint,b.category,'minute',
          floor(extract(epoch FROM b.created_at)/60)::bigint::text,
          (b.created_at AT TIME ZONE 'Asia/Shanghai')::date,
          'freq:v:'||s.projection_hmac||':'||'m',TRUE,
          date_trunc('minute',b.created_at)+interval '1 minute'
        FROM sms_message m
        JOIN sms_batch b ON b.id=m.batch_id AND b.category='verify'
        JOIN usage_frequency_subject s ON s.projection_hmac=m.phone_hmac
        WHERE b.usage_reservation_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO usage_frequency_entry(
          reservation_id,subject_id,app_id,category,window_kind,window_key,
          usage_date,projection_key,counted,expires_at
        )
        SELECT DISTINCT
          b.usage_reservation_id,s.id,
          CASE WHEN b.category='market' THEN COALESCE(b.app_id,0) ELSE NULL END,
          b.category,'day',
          to_char(b.created_at AT TIME ZONE 'Asia/Shanghai','YYYYMMDD'),
          (b.created_at AT TIME ZONE 'Asia/Shanghai')::date,
          CASE
            WHEN b.category='verify'
              THEN 'freq:v:'||s.projection_hmac||':'||'d'
            ELSE 'freq:m:'||COALESCE(b.app_id,0)::text||':'||
              s.projection_hmac||':'||'d'
          END,
          TRUE,
          ((((b.created_at AT TIME ZONE 'Asia/Shanghai')::date+1)::timestamp)
            AT TIME ZONE 'Asia/Shanghai')
        FROM sms_message m
        JOIN sms_batch b ON b.id=m.batch_id AND b.category IN ('verify','market')
        JOIN usage_frequency_subject s ON s.projection_hmac=m.phone_hmac
        WHERE b.usage_reservation_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO usage_projection(
          dimension_key,kind,usage_date,window_key,value,version,expires_at
        )
        SELECT projection_key,kind,usage_date,window_key,sum(amount),
          nextval('usage_projection_version_seq'),max(expires_at)
        FROM (
          SELECT projection_key,'quota'::varchar kind,usage_date,
            to_char(usage_date,'YYYYMMDD') window_key,
            amount::bigint amount,expires_at
          FROM usage_quota_entry
          UNION ALL
          SELECT projection_key,'frequency'::varchar kind,usage_date,
            window_key,1::bigint amount,expires_at
          FROM usage_frequency_entry WHERE counted
        ) facts
        WHERE expires_at>now()
        GROUP BY projection_key,kind,usage_date,window_key
        ON CONFLICT(dimension_key) DO UPDATE SET
          value=EXCLUDED.value,
          usage_date=EXCLUDED.usage_date,
          window_key=EXCLUDED.window_key,
          version=nextval('usage_projection_version_seq'),
          expires_at=EXCLUDED.expires_at,
          updated_at=now()
        """
    )

    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_task_name")
    op.execute(
        """
        ALTER TABLE outbox_event ADD CONSTRAINT ck_outbox_task_name CHECK (
          task_name IN (
            'app.tasks.send.process_batch',
            'app.tasks.deliver_callback',
            'app.tasks.outbox.compensate_quota',
            'app.tasks.outbox.deliver_alert',
            'app.tasks.outbox.release_usage'
          )
        )
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_args_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event ADD CONSTRAINT ck_outbox_args_no_pii CHECK (
          (
            args::text !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name='app.tasks.send.process_batch'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.deliver_callback'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.deliver_alert'
              AND jsonb_array_length(args)=2
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND args->>1 IN ('wecom','smtp')
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND jsonb_array_length(args)=6
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND jsonb_typeof(args->1)='string'
              AND args->>1 !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              AND args->>2 IN ('verify','notice','market')
              AND args->>3 ~ '^[0-9]{8}$'
              AND jsonb_typeof(args->4)='number'
              AND args->>4 ~ '^[0-9]+$'
              AND args->>5 ~ (
                '^(batch[:][0-9a-f]{32}[:]cancelled|'
                || 'approval[:][1-9][0-9]*[:](rejected|expired))$'
              )
            )
            OR (
              task_name='app.tasks.outbox.release_usage'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ (
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
                || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              )
            )
          )
          AND args::text !~ (
            '"(phone|phones|mobile|mobiles|phone_enc|phone_hmac|'
            || 'content|body|secret|password)"[[:space:]]*:'
          )
        )
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_refs_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event ADD CONSTRAINT ck_outbox_refs_no_pii CHECK (
          (
            aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name='app.tasks.send.process_batch'
              AND aggregate_id ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND aggregate_id ~ '^([0-9a-f]{32}|[0-9]+)$'
            )
            OR (
              task_name IN (
                'app.tasks.deliver_callback',
                'app.tasks.outbox.deliver_alert'
              )
              AND aggregate_id ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.release_usage'
              AND aggregate_id ~ (
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
                || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              )
            )
          )
          AND (
            dedup_key !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR dedup_key ~ (
              '^(batch[.]ready[:][0-9a-f]{32}|'
              || 'scheduled[:][0-9a-f]{32}[:]ready|'
              || 'batch[:][0-9a-f]{32}[:]cancelled|'
              || 'approval[:][1-9][0-9]*[:](approved|rejected|expired)|'
              || 'callback[:][1-9][0-9]*[:]attempt[:][0-9]+|'
              || 'alert[:][1-9][0-9]*[:](wecom|smtp)|'
              || 'usage[.]release[:][0-9a-f]{8}-[0-9a-f]{4}-'
              || '[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-'
              || '[0-9a-f]{12})$'
            )
          )
        )
        """
    )
    op.execute(
        """
        GRANT SELECT,INSERT,UPDATE,DELETE ON
          usage_reservation,usage_frequency_subject,usage_frequency_alias,
          usage_quota_entry,usage_frequency_entry,usage_projection,
          usage_projection_drift TO sms_app
        """
    )
    op.execute("GRANT USAGE,SELECT ON SEQUENCE usage_projection_version_seq TO sms_app")
    op.execute(
        """
        REVOKE TRUNCATE ON
          usage_reservation,usage_frequency_subject,usage_frequency_alias,
          usage_quota_entry,usage_frequency_entry,usage_projection,
          usage_projection_drift FROM sms_app
        """
    )


def downgrade() -> None:
    """有未释放事实时拒绝回滚，避免恢复后把权威用量静默归零。"""

    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM usage_reservation WHERE state<>'released'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade usage fact ledger with active reservations';
          END IF;
          IF EXISTS (
            SELECT 1 FROM outbox_event
            WHERE task_name='app.tasks.outbox.release_usage'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade usage fact ledger with usage release events';
          END IF;
        END
        $downgrade$
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_task_name")
    op.execute(
        """
        ALTER TABLE outbox_event ADD CONSTRAINT ck_outbox_task_name CHECK (
          task_name IN (
            'app.tasks.send.process_batch',
            'app.tasks.deliver_callback',
            'app.tasks.outbox.compensate_quota',
            'app.tasks.outbox.deliver_alert'
          )
        )
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_args_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event ADD CONSTRAINT ck_outbox_args_no_pii CHECK (
          (
            args::text !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name='app.tasks.send.process_batch'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.deliver_callback'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.deliver_alert'
              AND jsonb_array_length(args)=2
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND args->>1 IN ('wecom','smtp')
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND jsonb_array_length(args)=6
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND jsonb_typeof(args->1)='string'
              AND args->>1 !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              AND args->>2 IN ('verify','notice','market')
              AND args->>3 ~ '^[0-9]{8}$'
              AND jsonb_typeof(args->4)='number'
              AND args->>4 ~ '^[0-9]+$'
              AND args->>5 ~ (
                '^(batch[:][0-9a-f]{32}[:]cancelled|'
                || 'approval[:][1-9][0-9]*[:](rejected|expired))$'
              )
            )
          )
          AND args::text !~ (
            '"(phone|phones|mobile|mobiles|phone_enc|phone_hmac|'
            || 'content|body|secret|password)"[[:space:]]*:'
          )
        )
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_refs_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event ADD CONSTRAINT ck_outbox_refs_no_pii CHECK (
          (
            aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name='app.tasks.send.process_batch'
              AND aggregate_id ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND aggregate_id ~ '^([0-9a-f]{32}|[0-9]+)$'
            )
            OR (
              task_name IN (
                'app.tasks.deliver_callback',
                'app.tasks.outbox.deliver_alert'
              )
              AND aggregate_id ~ '^[0-9]+$'
            )
          )
          AND (
            dedup_key !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR dedup_key ~ (
              '^(batch[.]ready[:][0-9a-f]{32}|'
              || 'scheduled[:][0-9a-f]{32}[:]ready|'
              || 'batch[:][0-9a-f]{32}[:]cancelled|'
              || 'approval[:][1-9][0-9]*[:](approved|rejected|expired)|'
              || 'callback[:][1-9][0-9]*[:]attempt[:][0-9]+|'
              || 'alert[:][1-9][0-9]*[:](wecom|smtp))$'
            )
          )
        )
        """
    )
    op.drop_index("uk_sms_batch_usage_reservation", table_name="sms_batch")
    op.drop_column("sms_batch", "usage_reservation_id")
    op.drop_table("usage_projection_drift")
    op.drop_table("usage_projection")
    op.drop_index(
        "idx_usage_frequency_subject_window",
        table_name="usage_frequency_entry",
    )
    op.drop_table("usage_frequency_entry")
    op.drop_table("usage_quota_entry")
    op.drop_table("usage_frequency_alias")
    op.drop_table("usage_frequency_subject")
    op.drop_index("idx_usage_reservation_retention", table_name="usage_reservation")
    op.drop_index("idx_usage_reservation_recovery", table_name="usage_reservation")
    op.drop_index("uk_usage_reservation_active_request", table_name="usage_reservation")
    op.drop_table("usage_reservation")
    op.execute("DROP SEQUENCE usage_projection_version_seq")
    op.execute(
        """
        DELETE FROM sys_config
        WHERE key IN (
          'usage_projection_reconcile_seconds',
          'usage_ledger_retention_days'
        )
        """
    )
