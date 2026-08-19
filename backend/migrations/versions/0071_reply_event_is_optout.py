"""reply_event.is_optout：退订语判定前置到入库，支撑回复查询处置筛选。"""

from __future__ import annotations

from alembic import op

revision = "0071_reply_event_is_optout"
down_revision = "0070_stat_dirty_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线建库已含该列；存量升级在此补齐，两条路径幂等收敛。
    # 存量行无法在不解密的前提下补判，统一保持 FALSE。
    op.execute(
        """
        ALTER TABLE reply_event
          ADD COLUMN IF NOT EXISTS is_optout BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reply_event DROP COLUMN IF EXISTS is_optout")
