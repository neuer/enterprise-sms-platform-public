"""分片未受理确认事实绑定处置代次；不推断历史确认授权。"""
from __future__ import annotations

from alembic import op

revision = "0116_usage_release_generation"
down_revision = "0115_idempotency_result_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_chunk_release ADD COLUMN IF NOT EXISTS "
               "effect_generation INTEGER CHECK (effect_generation > 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE usage_chunk_release DROP COLUMN IF EXISTS effect_generation")
