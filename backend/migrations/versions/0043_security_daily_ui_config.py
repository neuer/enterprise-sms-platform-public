"""允许管理员在安全日报页面配置 Resend Key 与收件人。"""

from __future__ import annotations

from alembic import op

revision = "0043_security_daily_ui_config"
down_revision = "0042_security_daily_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐 UI 配置存储，并让 accept/send 角色按最小范围访问。"""

    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description)
        VALUES(
          'security_daily_resend_api_key','', 'str',
          '安全日报 Resend API Key（管理员配置页）'
        )
        ON CONFLICT(key) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS security_daily_recipient (
            position    SMALLINT PRIMARY KEY CHECK (position BETWEEN 1 AND 3),
            address     VARCHAR(254) NOT NULL CHECK (address <> ''),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_security_daily_recipient_address
        ON security_daily_recipient (lower(address))
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE, UPDATE ON security_daily_recipient TO sms_accept")
    op.execute("GRANT SELECT ON security_daily_recipient TO sms_send")


def downgrade() -> None:
    """撤销 UI 配置表和新增配置键。"""

    op.execute("REVOKE SELECT ON security_daily_recipient FROM sms_send")
    op.execute(
        "REVOKE SELECT, INSERT, DELETE, UPDATE ON security_daily_recipient FROM sms_accept"
    )
    op.execute("DROP TABLE IF EXISTS security_daily_recipient")
    op.execute("DELETE FROM sys_config WHERE key='security_daily_resend_api_key'")
