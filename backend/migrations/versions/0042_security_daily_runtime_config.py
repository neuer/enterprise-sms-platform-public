"""为增量升级的数据库补齐安全日报运行配置默认值。"""

from __future__ import annotations

from alembic import op

revision = "0042_security_daily_runtime_config"
down_revision = "0041_security_daily_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """幂等回填安全日报配置，保留已有管理员修改的值。"""

    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description) VALUES
          (
            'security_daily_enabled','false','bool',
            '服务器安全日报生成与手动投递开关'
          ),
          (
            'security_daily_recipient_count','0','int',
            '独立 mailer 当前收件人数，仅保存数量'
          ),
          (
            'security_daily_resend_configured','false','bool',
            '独立 mailer Resend Key 与收件人配置状态'
          )
        ON CONFLICT(key) DO NOTHING
        """
    )


def downgrade() -> None:
    """删除本迁移补入的配置键。"""

    op.execute(
        """
        DELETE FROM sys_config
        WHERE key IN (
          'security_daily_enabled',
          'security_daily_recipient_count',
          'security_daily_resend_configured'
        )
        """
    )
