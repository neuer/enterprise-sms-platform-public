"""允许 Outbox 将报告轮询投递到独立 realtime-report 队列。"""

from __future__ import annotations

from alembic import op

revision = "0082_outbox_realtime_report_queue"
down_revision = "0081_sign_adoption_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          ALTER TABLE outbox_event
            RENAME CONSTRAINT outbox_event_queue_check TO ck_outbox_queue;
        EXCEPTION
          WHEN undefined_object THEN NULL;
        END;
        $$
        """
    )
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_queue"
    )
    op.execute(
        """
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_queue CHECK (
          queue IN ('realtime','realtime-report','bulk','callback')
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM outbox_event WHERE queue='realtime-report'
          ) THEN
            RAISE EXCEPTION
              'cannot remove realtime-report while outbox events still reference it';
          END IF;
        END;
        $$
        """
    )
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_queue"
    )
    op.execute(
        """
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_queue CHECK (
          queue IN ('realtime','bulk','callback')
        )
        """
    )
