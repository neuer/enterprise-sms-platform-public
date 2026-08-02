"""安全日报记录增加自动/手动生成来源字段。"""

from __future__ import annotations

from alembic import op

revision = "0045_security_daily_generation_source"
down_revision = "0044_security_daily_audit_view"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增来源列；存量记录按自动生成回填。"""

    op.execute(
        """
        ALTER TABLE security_daily_report
        ADD COLUMN IF NOT EXISTS generation_source VARCHAR(8) NOT NULL DEFAULT 'auto'
        CHECK (generation_source IN ('auto','manual'))
        """
    )


def downgrade() -> None:
    """撤销来源字段；不影响报告事实与投递记录。"""

    op.execute(
        """
        ALTER TABLE security_daily_report
        DROP CONSTRAINT IF EXISTS ck_security_daily_generation_source
        """
    )
    op.execute(
        "ALTER TABLE security_daily_report DROP COLUMN IF EXISTS generation_source"
    )
