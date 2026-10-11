"""轮询续租不确定原因的持久化计数；沿用已有最小权限。"""

from alembic import op

revision = "0122_poll_lease_events"
down_revision = "0121_audit_payload_shape"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE worker_lease_event "
        "DROP CONSTRAINT IF EXISTS worker_lease_event_task_kind_check"
    )
    op.execute("""
        ALTER TABLE worker_lease_event ADD CONSTRAINT worker_lease_event_task_kind_check
        CHECK (task_kind IN ('callback','export','raw','poll_report','poll_reply'))
    """)
    op.execute(
        "ALTER TABLE worker_lease_event "
        "DROP CONSTRAINT IF EXISTS worker_lease_event_event_type_check"
    )
    op.execute("""
        ALTER TABLE worker_lease_event ADD CONSTRAINT worker_lease_event_event_type_check
        CHECK (event_type IN (
          'acquired','takeover','heartbeat_lost','fencing_miss','dead','manual_retry',
          'extend_rejected','owner_lost','renewal_unavailable',
          'release_unavailable','deadline_elapsed'
        ))
    """)


def downgrade() -> None:
    # 应用回退保留已追加的故障事实与兼容枚举，禁止删除事实以缩窄约束。
    pass
