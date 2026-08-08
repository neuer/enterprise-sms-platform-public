"""Web 幂等记录按稳定账号/身份隔离作用域。"""

from __future__ import annotations

from alembic import op

revision = "0053_idempotency_scope"
down_revision = "0052_idempotency_request_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线（0001）已包含这些列，这里必须幂等，与既有 add-column 迁移惯例一致。
    op.execute(
        "ALTER TABLE idempotency_record ADD COLUMN IF NOT EXISTS scope_kind VARCHAR(16)"
    )
    op.execute(
        "ALTER TABLE idempotency_record ADD COLUMN IF NOT EXISTS scope_id VARCHAR(64)"
    )
    op.execute(
        "UPDATE idempotency_record SET scope_kind='app', scope_id=app_id::text "
        "WHERE app_id IS NOT NULL AND scope_kind IS NULL"
    )
    op.execute(
        "UPDATE idempotency_record SET scope_kind='web-legacy', scope_id='global' "
        "WHERE app_id IS NULL AND scope_kind IS NULL"
    )
    op.execute(
        "ALTER TABLE idempotency_record ALTER COLUMN scope_kind SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE idempotency_record ALTER COLUMN scope_id SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE idempotency_record DROP CONSTRAINT IF EXISTS uk_idem_app_biz"
    )
    op.execute(
        "ALTER TABLE idempotency_record ADD CONSTRAINT uk_idem_app_biz "
        "UNIQUE (scope_kind, scope_id, biz_id)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE idempotency_record DROP CONSTRAINT IF EXISTS uk_idem_app_biz"
    )
    op.execute(
        "ALTER TABLE idempotency_record ADD CONSTRAINT uk_idem_app_biz "
        "UNIQUE NULLS NOT DISTINCT (app_id, biz_id)"
    )
    op.execute(
        "ALTER TABLE idempotency_record ALTER COLUMN scope_id DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE idempotency_record ALTER COLUMN scope_kind DROP NOT NULL"
    )
    op.execute("ALTER TABLE idempotency_record DROP COLUMN scope_kind")
    op.execute("ALTER TABLE idempotency_record DROP COLUMN scope_id")
