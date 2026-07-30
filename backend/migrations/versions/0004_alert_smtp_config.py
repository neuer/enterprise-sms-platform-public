"""增加告警 SMTP 无认证内网 relay 的非敏感路由配置。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_alert_smtp_config"
down_revision: str | None = "0003_sign_vendor_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description) VALUES
          ('alert_smtp_host','smtp','str','告警邮件内网relay主机'),
          ('alert_smtp_port','25','int','告警邮件内网relay端口'),
          ('alert_mail_from','sms-platform@localhost','str','告警邮件发件人')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM sys_config WHERE key IN "
        "('alert_smtp_host','alert_smtp_port','alert_mail_from')"
    )
