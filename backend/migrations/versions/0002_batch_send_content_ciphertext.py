"""为可靠重投保存实际下发内容密文。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_batch_content_enc"
down_revision: str | None = "0001_schema_v16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增版本头+AES-GCM密文；兼容迁移时可能存在的历史空批次。"""

    op.execute("ALTER TABLE sms_batch ADD COLUMN IF NOT EXISTS send_content_enc BYTEA")
    op.execute(
        "UPDATE sms_batch SET send_content_enc = decode('0000', 'hex') "
        "WHERE send_content_enc IS NULL"
    )
    op.alter_column("sms_batch", "send_content_enc", nullable=False)


def downgrade() -> None:
    op.drop_column("sms_batch", "send_content_enc")
