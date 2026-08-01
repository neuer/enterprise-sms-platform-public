"""补齐 bulk worker 的导入与生命周期清理最小权限。"""

from __future__ import annotations

from alembic import op

revision = "0040_background_task_role_matrix"
down_revision = "0039_manual_job_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """让异步导入和 housekeeping 使用既有 sms_send 连接完成其 DML。"""

    op.execute("GRANT SELECT ON user_account, import_task, import_phone TO sms_send")
    op.execute("GRANT UPDATE, DELETE ON import_task TO sms_send")
    op.execute("GRANT INSERT, DELETE ON import_phone TO sms_send")
    op.execute(
        "GRANT DELETE ON idempotency_record, callback_task, callback_report_event TO sms_send"
    )
    op.execute("GRANT USAGE, SELECT ON SEQUENCE import_phone_id_seq TO sms_send")


def downgrade() -> None:
    """回退仍保持 worker 对导入和清理事实源 fail closed。"""

    op.execute(
        "REVOKE DELETE ON idempotency_record, callback_task, callback_report_event FROM sms_send"
    )
    op.execute("REVOKE INSERT, DELETE ON import_phone FROM sms_send")
    op.execute("REVOKE UPDATE, DELETE ON import_task FROM sms_send")
    op.execute("REVOKE SELECT ON user_account, import_task, import_phone FROM sms_send")
    op.execute("REVOKE USAGE, SELECT ON SEQUENCE import_phone_id_seq FROM sms_send")
