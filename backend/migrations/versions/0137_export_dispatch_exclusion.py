"""导出按消息引用读取下发前剔除原因，不授予剔除事实写入权。"""

from alembic import op

revision = "0137_export_dispatch_exclusion"
down_revision = "0136_dispatch_exclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只补齐导出查询实际使用的三列。"""
    op.execute(
        "GRANT SELECT (message_id, message_created_at, reason) "
        "ON sms_dispatch_exclusion TO sms_export"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (message_id, message_created_at, reason) "
        "ON sms_dispatch_exclusion FROM sms_export"
    )
