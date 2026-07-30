"""为 uncertain 分片记录准确状态进入时间。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_chunk_uncertain_since"
down_revision: str | None = "0006_user_directory_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sms_chunk ADD COLUMN IF NOT EXISTS uncertain_since TIMESTAMPTZ")


def downgrade() -> None:
    op.drop_column("sms_chunk", "uncertain_since")
