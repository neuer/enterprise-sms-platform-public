"""为增量升级的数据库补齐审批过期与定时批次扫描调度配置。"""

from __future__ import annotations

from alembic import op

revision = "0050_beat_scan_config"
down_revision = "0049_app_ip_allowlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """幂等回填两个 beat 扫描间隔配置，保留已有管理员修改的值。"""

    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description) VALUES
          (
            'approval_scan_seconds','300','int',
            '审批过期扫描间隔(秒,重启beat生效)'
          ),
          (
            'scheduled_scan_seconds','60','int',
            '定时批次扫描间隔(秒,重启beat生效)'
          )
        ON CONFLICT(key) DO NOTHING
        """
    )


def downgrade() -> None:
    """删除本迁移补入的配置键。"""

    op.execute(
        """
        DELETE FROM sys_config
        WHERE key IN ('approval_scan_seconds','scheduled_scan_seconds')
        """
    )
