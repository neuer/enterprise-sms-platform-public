"""阻断审计 JSON 中的手机号与逐号保护字段。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_audit_payload_guard"
down_revision: str | None = "0007_chunk_uncertain_since"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $audit_guard$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_audit_payload_no_pii'
              AND conrelid=CAST('audit_log' AS regclass)
          ) THEN
            ALTER TABLE audit_log ADD CONSTRAINT ck_audit_payload_no_pii CHECK (
              (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
                !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              AND (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
                !~ '"(phones|mobiles|phone_enc|phone_hmac|phone_list|mobile_list)"[[:space:]]*:'
            );
          END IF;
        END
        $audit_guard$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS ck_audit_payload_no_pii")
