"""Outbox 业务等待计龄锚点；基础设施重试不得重置年龄。"""

from alembic import op

revision = "0125_outbox_capacity_clock"
down_revision = "0124_business_authorization_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    # NULL 即 created_at，不用迁移执行时间重写历史行；旧 writer 仍可安全工作。
    op.execute("""
        ALTER TABLE outbox_event ADD COLUMN IF NOT EXISTS capacity_eligible_at TIMESTAMPTZ
          CONSTRAINT ck_outbox_capacity_clock CHECK (
            capacity_eligible_at IS NULL OR (
              capacity_eligible_at >= created_at
              AND event_type IN ('callback.ready','chunk.ready')
            )
          )
    """)
    # 只回填可由任务的同事务时间、attempt 与未执行状态证明的合法回调等待。
    # 未知、已执行、基础设施失败和 authority-busy 全部保守维持 NULL。
    op.execute("""
        UPDATE outbox_event e SET capacity_eligible_at=c.next_attempt_at
        FROM (
        SELECT e.id,e.next_attempt_at FROM outbox_event e JOIN callback_task c
          ON e.aggregate_id=c.id::text
        WHERE e.capacity_eligible_at IS NULL
          AND e.state='pending' AND e.attempts=0 AND e.failure_count=0
          AND e.lease_id IS NULL AND e.next_attempt_at>now()
          AND e.event_type='callback.ready' AND e.task_name='app.tasks.deliver_callback'
          AND e.queue='callback' AND e.aggregate_type='callback_task'
          AND e.aggregate_id=c.id::text AND e.args=jsonb_build_array(c.id)
          AND c.status='retrying' AND c.retry_count BETWEEN 1 AND 5
          AND e.dedup_key='callback:'||c.id::text||':attempt:'||c.retry_count::text
          AND c.next_retry_at=e.next_attempt_at
          AND e.next_attempt_at-e.created_at IN (
            interval '60 seconds',interval '300 seconds',interval '900 seconds',
            interval '3600 seconds'
          )
        ORDER BY e.id LIMIT 10000
        ) c WHERE e.id=c.id AND e.capacity_eligible_at IS NULL
    """)
    op.execute("""
        UPDATE outbox_event e SET capacity_eligible_at=c.next_attempt_at
        FROM (
        SELECT id,next_attempt_at FROM outbox_event
        WHERE capacity_eligible_at IS NULL AND state='pending'
          AND event_type='chunk.ready' AND task_name='app.tasks.send.process_chunk'
          AND last_error='MarketWindowDeferred' AND lease_id IS NULL
          AND failure_count=0 AND attempts=0 AND next_attempt_at>now()
          AND next_attempt_at>=created_at
        ORDER BY id LIMIT 10000
        ) c WHERE e.id=c.id AND e.capacity_eligible_at IS NULL
    """)
    op.execute("GRANT SELECT (capacity_eligible_at) ON outbox_event TO sms_metrics")


def downgrade() -> None:
    # 应用回退保留业务时间证据与列权限，不删除事实或改变调度时间。
    pass
