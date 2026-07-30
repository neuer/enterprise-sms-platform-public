"""持久化分片重试到期时间并建立到期扫描索引。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_chunk_retry_not_before"
down_revision: str | None = "0010_chunk_submitting_since"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    column_names = {str(item["name"]) for item in inspector.get_columns("sms_chunk")}
    if "retry_not_before" not in column_names:
        op.add_column(
            "sms_chunk",
            sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
        )
    op.execute(
        "UPDATE sms_chunk SET retry_not_before=now() "
        "WHERE status='retrying' AND retry_not_before IS NULL"
    )
    index_names = {str(item["name"]) for item in inspector.get_indexes("sms_chunk")}
    if "idx_chunk_retry_due" not in index_names:
        op.create_index(
            "idx_chunk_retry_due",
            "sms_chunk",
            ["retry_not_before"],
            postgresql_where=sa.text("status = 'retrying'"),
        )


def downgrade() -> None:
    op.drop_index("idx_chunk_retry_due", table_name="sms_chunk")
    op.drop_column("sms_chunk", "retry_not_before")
