"""为异步导出 worker 增加可回收租约时间。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_export_started_at"
down_revision: str | None = "0004_alert_smtp_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE export_task ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ")


def downgrade() -> None:
    op.drop_column("export_task", "started_at")
