"""为真实联调测试号码增加跨 key 版本 HMAC 索引投影。"""

from __future__ import annotations

from alembic import op

revision = "0019_vendor_hmac_alias"
down_revision = "0018_vendor_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展并回填既有索引；不读取或解密手机号。"""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_test_recipient_hmac_alias (
          recipient_id BIGINT NOT NULL
            REFERENCES vendor_test_recipient(id) ON DELETE CASCADE,
          hmac_key_version SMALLINT NOT NULL CHECK (hmac_key_version > 0),
          hmac_digest CHAR(64) NOT NULL
            CHECK (hmac_digest ~ '^[0-9a-f]{64}$'),
          PRIMARY KEY (recipient_id, hmac_key_version),
          UNIQUE (hmac_key_version, hmac_digest)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vendor_test_recipient_hmac_alias_lookup
        ON vendor_test_recipient_hmac_alias(hmac_key_version, hmac_digest)
        """
    )
    op.execute(
        """
        INSERT INTO vendor_test_recipient_hmac_alias(
          recipient_id,hmac_key_version,hmac_digest
        )
        SELECT id,key_version,phone_hmac FROM vendor_test_recipient
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, DELETE "
        "ON vendor_test_recipient_hmac_alias TO sms_app"
    )
    op.execute(
        "REVOKE UPDATE, TRUNCATE "
        "ON vendor_test_recipient_hmac_alias FROM sms_app"
    )


def downgrade() -> None:
    """索引轮换证据不可由自动迁移删除。"""

    raise RuntimeError("vendor test recipient hmac alias downgrade is intentionally blocked")
