"""Persist delivery generation and recipient-set digest for security-daily."""

from __future__ import annotations

from alembic import op

revision = "0080_security_daily_delivery_generation"
down_revision = "0079_security_daily_publish_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE security_daily_report "
        "ADD COLUMN IF NOT EXISTS delivery_generation BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE security_daily_report "
        "DROP CONSTRAINT IF EXISTS ck_security_daily_report_delivery_generation"
    )
    op.execute(
        "ALTER TABLE security_daily_report "
        "ADD CONSTRAINT ck_security_daily_report_delivery_generation "
        "CHECK (delivery_generation >= 1)"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD COLUMN IF NOT EXISTS delivery_generation BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD COLUMN IF NOT EXISTS recipient_set_digest VARCHAR(64) NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "DROP CONSTRAINT IF EXISTS ck_security_daily_request_delivery_generation"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD CONSTRAINT ck_security_daily_request_delivery_generation "
        "CHECK (delivery_generation >= 1)"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "DROP CONSTRAINT IF EXISTS ck_security_daily_request_recipient_digest"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD CONSTRAINT ck_security_daily_request_recipient_digest "
        "CHECK (recipient_set_digest = '' OR recipient_set_digest ~ '^[0-9a-f]{64}$')"
    )


def downgrade() -> None:
    # 安全修复只允许前向演进；回退不得丢弃已确认的投递世代。
    pass
