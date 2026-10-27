"""批次终态汇总在入队时冻结，历史无快照事件不再按原身份重试。

从未投递过的 pending 任务可以按迁移当时的批次计数冻结，因为接收方还没有见过正文。
已经有过投递尝试、以及 done/dead 历史任务只写 legacy 标记，不复制当前计数。
这些仍在 pending/retrying 的旧任务改为 dead，停止按原 event_id 重试。
降级只删除快照表，不把已经停止的旧任务恢复成可重试。
"""

from __future__ import annotations

from alembic import op

revision = "0134_callback_batch_event"
down_revision = "0133_idempotency_sign_source"
branch_labels = None
depends_on = None

_TABLE = """
CREATE TABLE IF NOT EXISTS callback_batch_event (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL UNIQUE,
    batch_id BIGINT NOT NULL,
    source_report_event_key CHAR(64),
    recovered_from_task_id BIGINT UNIQUE,
    batch_no CHAR(32) NOT NULL,
    biz_id VARCHAR(32),
    category VARCHAR(8) NOT NULL,
    status VARCHAR(20),
    total INTEGER,
    delivered INTEGER,
    failed INTEGER,
    unknown_cnt INTEGER,
    manual_resolution_required BOOLEAN,
    finished_at TIMESTAMPTZ NOT NULL,
    legacy_unavailable BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_cbe_task FOREIGN KEY (task_id)
      REFERENCES callback_task(id) ON DELETE CASCADE,
    CONSTRAINT fk_cbe_batch FOREIGN KEY (batch_id)
      REFERENCES sms_batch(id) ON DELETE RESTRICT,
    CONSTRAINT fk_cbe_recovered_from FOREIGN KEY (recovered_from_task_id)
      REFERENCES callback_task(id) ON DELETE RESTRICT,
    CONSTRAINT fk_cbe_source_report FOREIGN KEY (source_report_event_key)
      REFERENCES report_event(event_key) ON DELETE RESTRICT,
    CONSTRAINT ck_cbe_recovery_identity CHECK (
      recovered_from_task_id IS NULL OR recovered_from_task_id <> task_id
    ),
    CONSTRAINT ck_cbe_payload CHECK (
      (
        legacy_unavailable
        AND status IS NULL
        AND total IS NULL
        AND delivered IS NULL
        AND failed IS NULL
        AND unknown_cnt IS NULL
        AND manual_resolution_required IS NULL
        AND recovered_from_task_id IS NULL
      )
      OR (
        NOT legacy_unavailable
        AND status IN (
          'completed','completed_unknown','cancelled','expired','rejected'
        )
        AND category IN ('verify','notice','market')
        AND total IS NOT NULL AND total >= 0
        AND delivered IS NOT NULL AND delivered >= 0
        AND failed IS NOT NULL AND failed >= 0
        AND unknown_cnt IS NOT NULL AND unknown_cnt >= 0
        AND manual_resolution_required = (status = 'completed_unknown')
      )
    )
)
"""

_FUNCTION = """
CREATE OR REPLACE FUNCTION reject_callback_batch_event_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'callback_batch_event is immutable';
END
$$
"""


def upgrade() -> None:
    """建表、授权，并区分从未投递与已经尝试过的历史终态。"""

    op.execute(_TABLE)
    op.execute(
        "COMMENT ON TABLE callback_batch_event IS "
        "'batch.finished 创建时冻结的汇总，不含手机号、正文或回调 body'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cbe_batch ON callback_batch_event(batch_id)"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_cbe_batch_source
          ON callback_batch_event(batch_id, source_report_event_key)
          WHERE source_report_event_key IS NOT NULL
        """
    )
    op.execute(_FUNCTION)
    op.execute(
        "REVOKE ALL ON FUNCTION reject_callback_batch_event_update() FROM PUBLIC"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_cbe_no_update ON callback_batch_event")
    op.execute(
        """
        CREATE TRIGGER trg_cbe_no_update
        BEFORE UPDATE ON callback_batch_event
        FOR EACH ROW EXECUTE FUNCTION reject_callback_batch_event_update()
        """
    )
    op.execute("REVOKE ALL ON callback_batch_event FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON callback_batch_event TO sms_accept")
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON callback_batch_event TO sms_send"
    )
    op.execute("GRANT SELECT, INSERT ON callback_batch_event TO sms_callback")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE callback_batch_event_id_seq "
        "TO sms_accept, sms_send, sms_callback"
    )
    op.execute(
        """
        INSERT INTO callback_batch_event(
          task_id,batch_id,source_report_event_key,batch_no,biz_id,category,
          status,total,delivered,failed,unknown_cnt,manual_resolution_required,
          finished_at,legacy_unavailable
        )
        SELECT t.id,b.id,t.source_report_event_key,trim(b.batch_no),b.biz_id,
          b.category,b.status,b.total,b.delivered,b.failed,b.unknown_cnt,
          b.status='completed_unknown',clock_timestamp(),false
        FROM callback_task t
        JOIN sms_batch b ON b.id=t.batch_id
        WHERE t.event='batch.finished'
          AND t.status='pending'
          AND t.retry_count=0
          AND t.last_http_code IS NULL
          AND t.last_error IS NULL
          AND t.lease_id IS NULL
          AND b.status IN (
            'completed','completed_unknown','cancelled','expired','rejected'
          )
          AND NOT EXISTS (
            SELECT 1 FROM callback_batch_event e WHERE e.task_id=t.id
          )
        """
    )
    # legacy marker：不复制当前计数，完成时间用任务创建时间。
    op.execute(
        """
        INSERT INTO callback_batch_event(
          task_id,batch_id,source_report_event_key,batch_no,biz_id,category,
          finished_at,legacy_unavailable
        )
        SELECT t.id,b.id,t.source_report_event_key,trim(b.batch_no),b.biz_id,
          b.category,t.created_at,true
        FROM callback_task t
        JOIN sms_batch b ON b.id=t.batch_id
        WHERE t.event='batch.finished'
          AND NOT EXISTS (
            SELECT 1 FROM callback_batch_event e WHERE e.task_id=t.id
          )
        """
    )
    op.execute(
        """
        UPDATE callback_task t
        SET status='dead',
          last_error='LegacySnapshotUnavailable',
          next_retry_at=NULL,
          lease_id=NULL,
          lease_expires_at=NULL,
          finished_at=COALESCE(t.finished_at, clock_timestamp())
        WHERE t.event='batch.finished'
          AND t.status IN ('pending','retrying')
          AND EXISTS (
            SELECT 1 FROM callback_batch_event e
            WHERE e.task_id=t.id AND e.legacy_unavailable
          )
        """
    )


def downgrade() -> None:
    """删除快照。已经因缺少快照而停止的旧任务保持 dead。"""

    op.execute("DROP TRIGGER IF EXISTS trg_cbe_no_update ON callback_batch_event")
    op.execute("DROP FUNCTION IF EXISTS reject_callback_batch_event_update()")
    op.execute("DROP TABLE IF EXISTS callback_batch_event")
