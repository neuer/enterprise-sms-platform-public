"""Close validated send, PII, mailer and callback security gaps."""

from __future__ import annotations

from alembic import op

revision = "0062_security_scan_remediations"
down_revision = "0061_vendor_binding_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧 SHA-256 指纹可能成为手机号/OTP 离线枚举 oracle；无法安全重签，立即清空。
    op.execute(
        "ALTER TABLE idempotency_record "
        "ADD COLUMN IF NOT EXISTS request_hash_key_version SMALLINT"
    )
    op.execute(
        "UPDATE idempotency_record SET request_hash=NULL,request_hash_key_version=NULL "
        "WHERE request_hash IS NOT NULL OR request_hash_key_version IS NOT NULL"
    )
    op.execute(
        """
        DO $constraint$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_idem_request_fingerprint'
              AND conrelid='idempotency_record'::regclass
          ) THEN
            ALTER TABLE idempotency_record
            ADD CONSTRAINT ck_idem_request_fingerprint CHECK (
              (request_hash IS NULL AND request_hash_key_version IS NULL)
              OR (
                request_hash ~ '^[0-9a-f]{64}$'
                AND request_hash_key_version BETWEEN 1 AND 32767
              )
            );
          END IF;
        END
        $constraint$
        """
    )

    # 独立 action 事实允许保留历史重复数据，同时让未来重发原子认领一次。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_resend_action (
          source_batch_id BIGINT PRIMARY KEY
            REFERENCES sms_batch(id) ON DELETE RESTRICT,
          child_batch_id BIGINT NOT NULL UNIQUE
            REFERENCES sms_batch(id) ON DELETE RESTRICT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT ck_sms_resend_action_distinct
            CHECK (source_batch_id <> child_batch_id)
        )
        """
    )
    op.execute(
        """
        INSERT INTO sms_resend_action(source_batch_id,child_batch_id)
        SELECT resend_of,min(id) FROM sms_batch
        WHERE resend_of IS NOT NULL GROUP BY resend_of
        ON CONFLICT(source_batch_id) DO NOTHING
        """
    )
    op.execute(
        "GRANT SELECT,INSERT ON sms_resend_action TO sms_accept"
    )

    # callback 仍可构造回调，但不能读取 send_content_enc 等短信正文密文。
    op.execute("REVOKE SELECT ON sms_batch FROM sms_callback")
    op.execute(
        "GRANT SELECT (id,batch_no,category,app_id,biz_id,status,total,"
        "delivered,failed,unknown_cnt,updated_at) ON sms_batch TO sms_callback"
    )

    # 把每个投递请求绑定到事务内读取的单调配置版本，mailer 版本不符即拒发。
    op.execute(
        "INSERT INTO sys_config(key,value,value_type,description) VALUES "
        "('security_daily_config_version','1','int','安全日报发信配置单调版本') "
        "ON CONFLICT(key) DO NOTHING"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ADD COLUMN IF NOT EXISTS config_version BIGINT"
    )
    op.execute(
        "UPDATE security_daily_delivery_request SET config_version=("
        "SELECT value::bigint FROM sys_config "
        "WHERE key='security_daily_config_version') WHERE config_version IS NULL"
    )
    op.execute(
        "ALTER TABLE security_daily_delivery_request "
        "ALTER COLUMN config_version SET NOT NULL"
    )
    op.execute(
        """
        DO $constraint$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_security_daily_request_config_version'
              AND conrelid='security_daily_delivery_request'::regclass
          ) THEN
            ALTER TABLE security_daily_delivery_request
            ADD CONSTRAINT ck_security_daily_request_config_version
            CHECK (config_version > 0);
          END IF;
        END
        $constraint$
        """
    )


def downgrade() -> None:
    # 安全修复只允许前向演进；回退不得重新开放摘要 oracle、重复发送或正文权限。
    pass
