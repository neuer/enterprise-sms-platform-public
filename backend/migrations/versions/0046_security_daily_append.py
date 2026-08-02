"""安全日报记录逐条保留：同一天允许多条记录，自动路径保持每天一条。"""

from __future__ import annotations

from alembic import op

revision = "0046_security_daily_append"
down_revision = "0045_security_daily_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """去掉 report_date 全局唯一约束，仅对自动生成保留每日一条的部分唯一索引。

    自动定时任务每天仍然只有一条 auto 记录（可从未 available 修复为 ready）；
    管理员“立即生成”每次插入新的 manual 记录，历史记录不再被覆盖。
    """

    op.execute(
        "ALTER TABLE security_daily_report "
        "DROP CONSTRAINT IF EXISTS security_daily_report_report_date_key"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS security_daily_report_report_date_key "
        "ON security_daily_report(report_date) "
        "WHERE generation_source='auto'"
    )


def downgrade() -> None:
    """撤销部分唯一索引并恢复每日一条约束；多记录数据需先清理才能回退。"""

    op.execute("DROP INDEX IF EXISTS security_daily_report_report_date_key")
    op.execute(
        """
        DELETE FROM security_daily_report
        WHERE id NOT IN (
            SELECT DISTINCT ON (report_date) id
            FROM security_daily_report
            ORDER BY report_date, id DESC
        )
        """
    )
    op.execute(
        """
        ALTER TABLE security_daily_report
        ADD CONSTRAINT security_daily_report_report_date_key UNIQUE (report_date)
        """
    )
