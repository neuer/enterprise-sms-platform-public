"""raw_vendor_log.capture_state 增加 protocol_invalid。"""

from __future__ import annotations

from alembic import op

revision = "0073_raw_protocol_invalid"
down_revision = "0072_raw_capture_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_capture_state"
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD CONSTRAINT ck_raw_vendor_capture_state
          CHECK (capture_state IN ('complete','complete_too_large','truncated','protocol_invalid'))
        """
    )


def downgrade() -> None:
    op.execute(
        "UPDATE raw_vendor_log SET capture_state='truncated' "
        "WHERE capture_state='protocol_invalid'"
    )
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_capture_state"
    )
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD CONSTRAINT ck_raw_vendor_capture_state
          CHECK (capture_state IN ('complete','complete_too_large','truncated'))
        """
    )
