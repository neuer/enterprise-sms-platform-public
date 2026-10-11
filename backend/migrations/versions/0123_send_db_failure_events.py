"""发送事务失败的固定分类事实；跨 worker 可读且不保存 SQL/厂商响应。"""

from alembic import op

revision = "0123_send_db_failure_events"
down_revision = "0122_poll_lease_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS send_db_failure_event (
            id UUID PRIMARY KEY,
            phase VARCHAR(16) NOT NULL CHECK (phase IN ('claim','finalize')),
            reason VARCHAR(24) NOT NULL CHECK (reason IN (
                'cas_lost','db_deadlock','db_timeout','db_connection','unexpected'
            )),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_send_db_failure_event_metrics
        ON send_db_failure_event(phase,reason)
    """)
    op.execute("REVOKE ALL ON send_db_failure_event FROM PUBLIC")
    op.execute("GRANT INSERT ON send_db_failure_event TO sms_send")
    op.execute("GRANT SELECT (phase,reason) ON send_db_failure_event TO sms_metrics")


def downgrade() -> None:
    # 回退应用保留故障事实；不删除观测证据。
    pass
