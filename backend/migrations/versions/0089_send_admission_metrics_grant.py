"""指标身份可读 Outbox created_at，用于最老积压年龄。"""

from __future__ import annotations

from alembic import op

revision = "0089_send_admission_metrics_grant"
down_revision = "0088_app_admission_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT (created_at)
          ON outbox_event TO sms_metrics
        """
    )


def downgrade() -> None:
    op.execute("REVOKE SELECT (created_at) ON outbox_event FROM sms_metrics")
