"""安全日报自动投递路径补充 sms_send 投递请求表授权。"""

from __future__ import annotations

from alembic import op

revision = "0051_security_daily_delivery_send"
down_revision = "0050_beat_scan_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许 bulk worker（sms_send）在自动生成日报后写入并更新投递请求。

    自动日报任务运行在 bulk 队列，由 sms_send 数据库身份执行；此前该身份只有
    security_daily_report 的写入权限，缺少 security_daily_delivery_request
    权限，导致每天 08:00 生成成功后提交自动投递时 permission denied。
    """

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON security_daily_delivery_request TO sms_send"
    )


def downgrade() -> None:
    """撤销 bulk worker 对投递请求表的写权限；只影响自动投递路径。"""

    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON security_daily_delivery_request FROM sms_send"
    )
