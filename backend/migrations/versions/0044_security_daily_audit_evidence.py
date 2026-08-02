"""安全日报管理审计证据只读视图（最小授权）。"""

from __future__ import annotations

from alembic import op

revision = "0044_security_daily_audit_evidence"
down_revision = "0043_security_daily_ui_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建不含审计载荷列的只读视图，仅授予生成日报的 sms_send。"""

    op.execute(
        """
        CREATE VIEW security_daily_audit_evidence AS
        SELECT created_at, actor, actor_subject_kind, role, ip, action,
               object_type, object_id
        FROM audit_log
        """
    )
    op.execute("GRANT SELECT ON security_daily_audit_evidence TO sms_send")


def downgrade() -> None:
    """撤销只读视图；只影响日报证据接入，不触碰审计主表。"""

    op.execute("REVOKE SELECT ON security_daily_audit_evidence FROM sms_send")
    op.execute("DROP VIEW security_daily_audit_evidence")
