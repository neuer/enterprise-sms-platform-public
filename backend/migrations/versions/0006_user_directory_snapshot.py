"""保存用户最近一次目录认证的来源组快照。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_user_directory_snapshot"
down_revision: str | None = "0005_export_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE IF EXISTS sys_user "
        "ADD COLUMN IF NOT EXISTS source_groups TEXT[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE IF EXISTS sys_user "
        "ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS sys_user DROP COLUMN IF EXISTS last_synced_at")
    op.execute("ALTER TABLE IF EXISTS sys_user DROP COLUMN IF EXISTS source_groups")
