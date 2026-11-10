"""为无主报告的报告时间查询增加索引；保留入库时间索引用于留存。"""

from alembic import op

revision = "0138_unmatched_report_time"
down_revision = "0137_export_dispatch_exclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅新增查询索引，不改写历史时间、业务数据或角色权限。"""
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_unmatched_report_time ON unmatched_report(report_time, id) "
        "WHERE report_time IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_unmatched_report_time")
