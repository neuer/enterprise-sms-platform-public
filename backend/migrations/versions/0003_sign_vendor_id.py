"""为签名状态同步保存厂商签名编号。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_sign_vendor_id"
down_revision: str | None = "0002_batch_content_enc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sms_sign ADD COLUMN IF NOT EXISTS vendor_sign_id VARCHAR(64)")


def downgrade() -> None:
    op.drop_column("sms_sign", "vendor_sign_id")
