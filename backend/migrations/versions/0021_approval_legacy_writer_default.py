"""让升级前 writer 继续创建可判定来源的审批记录。"""

from __future__ import annotations

from alembic import op

revision = "0021_approval_legacy_default"
down_revision = "0020_approval_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """省略快照字段的旧 writer 必须落为 legacy_unknown。"""

    op.execute(
        "ALTER TABLE approval ALTER COLUMN trigger_threshold_source "
        "SET DEFAULT 'legacy_unknown'"
    )


def downgrade() -> None:
    """恢复 0020 首次发布时的默认值。"""

    op.execute(
        "ALTER TABLE approval ALTER COLUMN trigger_threshold_source "
        "SET DEFAULT 'snapshot'"
    )
