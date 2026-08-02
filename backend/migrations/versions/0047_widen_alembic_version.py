"""放宽 Alembic 版本号列，避免迁移 revision 名称触顶 32 字符。"""

from __future__ import annotations

from alembic import op

revision = "0047_widen_alembic_version"
down_revision = "0046_security_daily_append"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """把 alembic_version.version_num 扩到 VARCHAR(64)。

    Alembic 自建的控制表只有一行；加宽只扩大容量，不改变任何业务语义。
    """

    op.execute(
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(64)"
    )


def downgrade() -> None:
    """收缩回 32 字符；当前 revision 超过 32 字符时 PostgreSQL 会安全拒绝。"""

    op.execute(
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(32)"
    )
