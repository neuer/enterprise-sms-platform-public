"""增加真实联调页面的加密测试号码与无敏感操作状态。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_vendor_test_web_console"
down_revision: str | None = "0016_vendor_live_test_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只扩展加密号码事实源和无 PII 控制操作记录。"""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_test_recipient (
            id BIGSERIAL PRIMARY KEY,
            label VARCHAR(64) NOT NULL,
            phone_enc BYTEA NOT NULL,
            phone_hmac CHAR(64) NOT NULL,
            phone_mask VARCHAR(32) NOT NULL,
            key_version SMALLINT NOT NULL CHECK (key_version > 0),
            status VARCHAR(16) NOT NULL DEFAULT 'active'
              CHECK (status IN ('active','disabled')),
            created_by VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            disabled_by VARCHAR(64),
            disabled_at TIMESTAMPTZ,
            UNIQUE (key_version, phone_hmac),
            CONSTRAINT chk_vendor_test_recipient_disabled CHECK (
              (status = 'active' AND disabled_by IS NULL AND disabled_at IS NULL)
              OR (status = 'disabled' AND disabled_by IS NOT NULL AND disabled_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vendor_test_recipient_active
        ON vendor_test_recipient(id)
        WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_test_operation (
            id UUID PRIMARY KEY,
            operation_type VARCHAR(32) NOT NULL
              CHECK (operation_type IN (
                'install_credentials','rotate_credentials','activate','pause','resume','uat_send'
              )),
            actor VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL
              CHECK (status IN ('requested','running','succeeded','failed')),
            safe_code VARCHAR(64),
            batch_no VARCHAR(64),
            checkpoint_id VARCHAR(128),
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT chk_vendor_test_operation_completion CHECK (
              (status IN ('requested','running') AND completed_at IS NULL)
              OR (status IN ('succeeded','failed') AND completed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vendor_test_operation_status_time
        ON vendor_test_operation(status, requested_at)
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON vendor_test_recipient TO sms_app"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE vendor_test_recipient_id_seq TO sms_app"
    )
    op.execute("REVOKE TRUNCATE ON vendor_test_recipient FROM sms_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON vendor_test_operation TO sms_app")
    op.execute("REVOKE DELETE, TRUNCATE ON vendor_test_operation FROM sms_app")


def downgrade() -> None:
    """联调号码和操作证据不可由自动迁移删除。"""

    raise RuntimeError("vendor test web console downgrade is permanently disabled")
