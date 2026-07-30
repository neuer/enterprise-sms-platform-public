"""增加数据库权威的用户会话版本。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_security_hardening"
down_revision: str | None = "0013_auth_runtime_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """兼容当前 schema 基线与尚未具备会话版本列的真实 0013 旧库。"""

    op.execute(
        "ALTER TABLE IF EXISTS sys_user "
        "ADD COLUMN IF NOT EXISTS auth_version BIGINT NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS sys_user DROP COLUMN IF EXISTS auth_version")
