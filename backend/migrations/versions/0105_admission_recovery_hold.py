"""Admission 首次 CLOSED 恢复必须保存 degraded/recovery_hold，禁止 open+future hold。"""

from __future__ import annotations

from alembic import op

revision = "0105_admission_recovery_hold"
down_revision = "0104_uncertain_web_usage_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        UPDATE send_admission_state
        SET state='degraded', reason_code='recovery_hold'
        WHERE state='open'
          AND hold_until IS NOT NULL
          AND hold_until > now()
        """
    )
    op.execute(
        """
        UPDATE send_admission_state
        SET hold_until=NULL
        WHERE state='open' AND hold_until IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE send_admission_state
        SET state='degraded'
        WHERE reason_code='recovery_hold'
          AND state <> 'degraded'
        """
    )
    op.execute(
        """
        ALTER TABLE send_admission_state
          DROP CONSTRAINT IF EXISTS ck_send_admission_open_without_hold
        """
    )
    op.execute(
        """
        ALTER TABLE send_admission_state
          ADD CONSTRAINT ck_send_admission_open_without_hold
          CHECK (state <> 'open' OR hold_until IS NULL)
        """
    )
    op.execute(
        """
        ALTER TABLE send_admission_state
          DROP CONSTRAINT IF EXISTS ck_send_admission_recovery_hold_state
        """
    )
    op.execute(
        """
        ALTER TABLE send_admission_state
          ADD CONSTRAINT ck_send_admission_recovery_hold_state
          CHECK (reason_code <> 'recovery_hold' OR state = 'degraded')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE send_admission_state
          DROP CONSTRAINT IF EXISTS ck_send_admission_recovery_hold_state
        """
    )
    op.execute(
        """
        ALTER TABLE send_admission_state
          DROP CONSTRAINT IF EXISTS ck_send_admission_open_without_hold
        """
    )
    return
