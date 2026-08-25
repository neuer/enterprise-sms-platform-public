"""Persist security-daily config publish outbox and unknown delivery state."""

from __future__ import annotations

from alembic import op

revision = "0079_security_daily_publish_outbox"
down_revision = "0078_system_raw_replay_audit_producer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO sys_config(key,value,value_type,description) VALUES "
        "('security_daily_config_publish_state','file_committed','str',"
        "'安全日报配置发布状态'),"
        "('security_daily_config_file_version','1','int',"
        "'安全日报已发布到 mailer 文件的配置版本'),"
        "('security_daily_config_operation_id','','str',"
        "'安全日报最近一次配置发布操作标识') "
        "ON CONFLICT(key) DO NOTHING"
    )
    op.execute(
        "ALTER TABLE security_daily_report "
        "DROP CONSTRAINT IF EXISTS security_daily_report_delivery_status_check"
    )
    op.execute(
        "ALTER TABLE security_daily_report "
        "ADD CONSTRAINT security_daily_report_delivery_status_check "
        "CHECK (delivery_status IN ("
        "'not_sent','pending','sending','sent','failed','unknown'))"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "DROP CONSTRAINT IF EXISTS security_daily_delivery_request_state_check"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD CONSTRAINT security_daily_delivery_request_state_check "
        "CHECK (state IN ('pending','sent','failed','unknown'))"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "DROP CONSTRAINT IF EXISTS ck_security_daily_request_completion"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD CONSTRAINT ck_security_daily_request_completion CHECK ("
        "(state='pending' AND completed_at IS NULL) "
        "OR (state IN ('sent','failed','unknown') AND completed_at IS NOT NULL))"
    )


def downgrade() -> None:
    # 安全修复只允许前向演进；回退不得重新允许误标 Failed 或版本回退覆盖。
    pass
