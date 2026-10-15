"""观测明细有界压缩，保留固定维度的历史累计值与只追加权限。"""

from alembic import op

revision = "0128_runtime_event_retention"
down_revision = "0127_callback_app_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS worker_lease_event_rollup (
            task_kind VARCHAR(16) NOT NULL CHECK (task_kind IN (
                'callback','export','raw','poll_report','poll_reply'
            )),
            event_type VARCHAR(24) NOT NULL CHECK (event_type IN (
                'acquired','takeover','heartbeat_lost','fencing_miss','dead','manual_retry',
                'extend_rejected','owner_lost','renewal_unavailable',
                'release_unavailable','deadline_elapsed'
            )),
            event_count BIGINT NOT NULL CHECK (event_count>=0),
            PRIMARY KEY (task_kind,event_type)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS send_db_failure_event_rollup (
            phase VARCHAR(16) NOT NULL CHECK (phase IN ('claim','finalize')),
            reason VARCHAR(24) NOT NULL CHECK (reason IN (
                'cas_lost','db_deadlock','db_timeout','db_connection','unexpected'
            )),
            event_count BIGINT NOT NULL CHECK (event_count>=0),
            PRIMARY KEY (phase,reason)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_worker_lease_event_retention
        ON worker_lease_event(created_at,id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_send_db_failure_event_retention
        ON send_db_failure_event(created_at,id)
    """)
    op.execute("""
        INSERT INTO sys_config(key,value,value_type,description)
        VALUES('runtime_event_retention_days','30','int','运行观测事件明细保留天数；累计值长期保留')
        ON CONFLICT (key) DO NOTHING
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION compact_runtime_events(
            p_stream text, p_cutoff timestamptz, p_limit integer
        ) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE
            retention_text text;
            retention_days integer;
            expiry timestamptz;
            affected bigint;
        BEGIN
            IF p_stream IS NULL OR p_stream NOT IN ('worker_lease','send_db_failure')
               OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000
               OR p_cutoff IS NULL OR NOT isfinite(p_cutoff)
               OR p_cutoff>clock_timestamp() THEN
                RAISE EXCEPTION 'invalid runtime event compaction request' USING ERRCODE='22023';
            END IF;
            SELECT value INTO retention_text FROM public.sys_config
            WHERE key='runtime_event_retention_days';
            IF retention_text IS NULL OR retention_text !~ '^[0-9]{1,3}$' THEN
                RAISE EXCEPTION 'invalid runtime event retention policy' USING ERRCODE='22023';
            END IF;
            retention_days := retention_text::integer;
            IF retention_days NOT BETWEEN 1 AND 365 THEN
                RAISE EXCEPTION 'invalid runtime event retention policy' USING ERRCODE='22023';
            END IF;
            expiry := p_cutoff-make_interval(days=>retention_days);
            IF p_stream='worker_lease' THEN
                WITH due AS MATERIALIZED (
                    SELECT id FROM public.worker_lease_event WHERE created_at<expiry
                    ORDER BY created_at,id LIMIT p_limit FOR UPDATE SKIP LOCKED
                ), removed AS (
                    DELETE FROM public.worker_lease_event e USING due
                    WHERE e.id=due.id RETURNING e.task_kind,e.event_type
                ), accumulated AS (
                    INSERT INTO public.worker_lease_event_rollup(task_kind,event_type,event_count)
                    SELECT task_kind,event_type,count(*) FROM removed
                    GROUP BY task_kind,event_type ORDER BY task_kind,event_type
                    ON CONFLICT (task_kind,event_type) DO UPDATE
                    SET event_count=public.worker_lease_event_rollup.event_count
                        +EXCLUDED.event_count
                    RETURNING event_count
                )
                SELECT count(*) INTO affected FROM removed
                WHERE (SELECT count(*) FROM accumulated)>=0;
            ELSE
                WITH due AS MATERIALIZED (
                    SELECT id FROM public.send_db_failure_event WHERE created_at<expiry
                    ORDER BY created_at,id LIMIT p_limit FOR UPDATE SKIP LOCKED
                ), removed AS (
                    DELETE FROM public.send_db_failure_event e USING due
                    WHERE e.id=due.id RETURNING e.phase,e.reason
                ), accumulated AS (
                    INSERT INTO public.send_db_failure_event_rollup(phase,reason,event_count)
                    SELECT phase,reason,count(*) FROM removed
                    GROUP BY phase,reason ORDER BY phase,reason
                    ON CONFLICT (phase,reason) DO UPDATE
                    SET event_count=public.send_db_failure_event_rollup.event_count
                        +EXCLUDED.event_count
                    RETURNING event_count
                )
                SELECT count(*) INTO affected FROM removed
                WHERE (SELECT count(*) FROM accumulated)>=0;
            END IF;
            RETURN affected;
        END
        $$
    """)
    op.execute("""
        REVOKE ALL ON worker_lease_event_rollup,send_db_failure_event_rollup FROM PUBLIC
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION compact_runtime_events(text,timestamptz,integer) FROM PUBLIC
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION compact_runtime_events(text,timestamptz,integer) TO sms_send
    """)
    op.execute("""
        GRANT SELECT(task_kind,event_type,event_count) ON worker_lease_event_rollup TO sms_metrics
    """)
    op.execute("""
        GRANT SELECT(phase,reason,event_count) ON send_db_failure_event_rollup TO sms_metrics
    """)


def downgrade() -> None:
    # 空归档兼容旧降级保护；已经压缩后旧 reader 无法重建历史，必须前滚。
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_lease_event_rollup WHERE event_count>0)
               OR EXISTS (SELECT 1 FROM send_db_failure_event_rollup WHERE event_count>0) THEN
                RAISE EXCEPTION 'runtime event rollup requires a compatible reader';
            END IF;
        END $$
    """)
