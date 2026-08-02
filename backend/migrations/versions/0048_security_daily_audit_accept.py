"""安全日报审计证据视图补充 sms_accept 只读授权。"""

from __future__ import annotations

from alembic import op

revision = "0048_security_daily_audit_accept"
down_revision = "0047_widen_alembic_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许 API（sms_accept）在手动生成日报时读取脱敏审计摘要视图。"""

    op.execute("GRANT SELECT ON security_daily_audit_evidence TO sms_accept")


def downgrade() -> None:
    """撤销 API 的视图读权限；只影响日报审计摘要接入。"""

    op.execute("REVOKE SELECT ON security_daily_audit_evidence FROM sms_accept")
