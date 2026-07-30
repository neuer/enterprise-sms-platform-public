"""让 API 手动任务触发通过 PostgreSQL Outbox 跨越 broker 边界。"""

from __future__ import annotations

from alembic import op

revision = "0039_manual_job_outbox"
down_revision = "0038_raw_replay_processing_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只扩宽固定 task_name 集合，不改写或删除既有事件。"""

    op.execute(
        """
        ALTER TABLE outbox_event
        DROP CONSTRAINT IF EXISTS ck_outbox_task_name
        """
    )
    op.execute(
        """
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_task_name
        CHECK (task_name IN (
          'app.tasks.send.process_batch',
          'app.tasks.deliver_callback',
          'app.tasks.outbox.compensate_quota',
          'app.tasks.outbox.deliver_alert',
          'app.tasks.outbox.release_usage',
          'app.tasks.outbox.trigger_job'
        ))
        """
    )


def downgrade() -> None:
    """存在手动任务事件时拒绝收窄，避免遗失已受理操作。"""

    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM outbox_event
            WHERE task_name='app.tasks.outbox.trigger_job'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade manual job outbox with persisted events';
          END IF;
        END
        $downgrade$
        """
    )
    op.execute(
        """
        ALTER TABLE outbox_event
        DROP CONSTRAINT IF EXISTS ck_outbox_task_name
        """
    )
    op.execute(
        """
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_task_name
        CHECK (task_name IN (
          'app.tasks.send.process_batch',
          'app.tasks.deliver_callback',
          'app.tasks.outbox.compensate_quota',
          'app.tasks.outbox.deliver_alert',
          'app.tasks.outbox.release_usage'
        ))
        """
    )
