"""拆分容量滞回与定时保持的持久状态约束。"""

from alembic import op

revision = "0126_admission_recovery_mode"
down_revision = "0125_outbox_capacity_clock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT (scope) ON send_admission_state TO sms_metrics")
    op.execute("""
        UPDATE send_admission_state SET reason_code='capacity_hysteresis',
          state_epoch=state_epoch+1, updated_at=now()
        WHERE reason_code='recovery_hold' AND hold_until IS NULL
    """)
    op.execute("""
        ALTER TABLE send_admission_state
          DROP CONSTRAINT IF EXISTS ck_send_admission_recovery_hold_state
    """)
    op.execute("""
        ALTER TABLE send_admission_state
          DROP CONSTRAINT IF EXISTS ck_send_admission_hysteresis_state
    """)
    op.execute("""
        ALTER TABLE send_admission_state
          ADD CONSTRAINT ck_send_admission_recovery_hold_state CHECK (
            reason_code <> 'recovery_hold' OR (state='degraded' AND hold_until IS NOT NULL)
          )
    """)
    op.execute("""
        ALTER TABLE send_admission_state
          ADD CONSTRAINT ck_send_admission_hysteresis_state CHECK (
            reason_code <> 'capacity_hysteresis' OR state IN ('closed','degraded')
          )
    """)


def downgrade() -> None:
    # 不回退控制面事实；旧 writer 无期限 recovery_hold 会安全失败关闭。
    pass
