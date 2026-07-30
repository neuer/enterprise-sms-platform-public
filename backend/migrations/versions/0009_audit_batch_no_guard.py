"""避免合法 batch_no 被手机号正则误判，同时保留审计 PII 阻断。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_audit_batch_no_guard"
down_revision: str | None = "0008_audit_payload_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT ck_audit_payload_no_pii")
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT ck_audit_payload_no_pii CHECK (
          (COALESCE((before_val - 'batch_no')::text,'') ||
           COALESCE((after_val - 'batch_no')::text,''))
            !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
          AND (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
            !~ '"(phones|mobiles|phone_enc|phone_hmac|phone_list|mobile_list)"[[:space:]]*:'
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT ck_audit_payload_no_pii")
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT ck_audit_payload_no_pii CHECK (
          (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
            !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
          AND (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
            !~ '"(phones|mobiles|phone_enc|phone_hmac|phone_list|mobile_list)"[[:space:]]*:'
        )
        """
    )
