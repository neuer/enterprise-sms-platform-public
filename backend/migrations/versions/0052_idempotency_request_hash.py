"""idempotency request fingerprint and scheduled window coverage."""

from __future__ import annotations

from alembic import op

revision = "0052_idempotency_request_hash"
down_revision = "0051_security_daily_delivery_send"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线（0001）已包含该列，这里必须幂等，与既有 add-column 迁移惯例一致。
    op.execute(
        "ALTER TABLE idempotency_record "
        "ADD COLUMN IF NOT EXISTS request_hash VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE idempotency_record ALTER COLUMN app_id DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE idempotency_record DROP CONSTRAINT IF EXISTS uk_idem_app_biz"
    )
    op.execute(
        "ALTER TABLE idempotency_record ADD CONSTRAINT uk_idem_app_biz "
        "UNIQUE NULLS NOT DISTINCT (app_id, biz_id)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE idempotency_record DROP CONSTRAINT IF EXISTS uk_idem_app_biz"
    )
    op.execute(
        "ALTER TABLE idempotency_record ADD CONSTRAINT uk_idem_app_biz "
        "UNIQUE (app_id, biz_id)"
    )
    op.execute(
        "ALTER TABLE idempotency_record ALTER COLUMN app_id SET NOT NULL"
    )
    op.execute("ALTER TABLE idempotency_record DROP COLUMN request_hash")
