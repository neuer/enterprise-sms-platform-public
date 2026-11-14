"""删除已下线真实联调的五张专用表；历史审计与短信事实保持不变。"""

from alembic import op

revision = "0139_retire_vendor_test_tables"
down_revision = "0138_unmatched_report_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """先删依赖表；不级联删除未知对象，重复执行不恢复已退役数据。"""
    op.execute("DROP TABLE IF EXISTS public.vendor_test_recipient_hmac_alias")
    op.execute("DROP TABLE IF EXISTS public.vendor_test_recipient")
    op.execute("DROP TABLE IF EXISTS public.vendor_test_send_attempt")
    op.execute("DROP TABLE IF EXISTS public.vendor_test_daily_usage")
    op.execute("DROP TABLE IF EXISTS public.vendor_test_operation")


def downgrade() -> None:
    """拒绝把空表伪装成已恢复的联调历史。"""
    raise RuntimeError("retired vendor test data cannot be downgraded")
