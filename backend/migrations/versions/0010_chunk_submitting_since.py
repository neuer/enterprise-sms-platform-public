"""为 submitting 分片记录准确状态进入时间。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_chunk_submitting_since"
down_revision: str | None = "0009_audit_batch_no_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    column_names = {str(item["name"]) for item in inspector.get_columns("sms_chunk")}
    if "submitting_since" not in column_names:
        op.add_column(
            "sms_chunk",
            sa.Column("submitting_since", sa.DateTime(timezone=True), nullable=True),
        )
    op.execute(
        "UPDATE sms_chunk SET submitting_since=now() "
        "WHERE status='submitting' AND submitting_since IS NULL"
    )
    index_names = {str(item["name"]) for item in inspector.get_indexes("sms_chunk")}
    if "idx_chunk_submitting" not in index_names:
        op.create_index(
            "idx_chunk_submitting",
            "sms_chunk",
            ["submitting_since"],
            postgresql_where=sa.text("status = 'submitting'"),
        )


def downgrade() -> None:
    op.drop_index("idx_chunk_submitting", table_name="sms_chunk")
    op.drop_column("sms_chunk", "submitting_since")
