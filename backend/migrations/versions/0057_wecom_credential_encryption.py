"""Encrypt newly configured WeCom credentials and remove legacy plaintext."""

from __future__ import annotations

from alembic import op

revision = "0057_wecom_credential_encryption"
down_revision = "0056_audit_attribution_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic does not receive the data AES key. Fail closed by removing any
    # reusable legacy plaintext; an administrator must configure a fresh key.
    op.execute(
        """
        UPDATE sys_config
        SET value='',updated_by='security-migration',updated_at=now()
        WHERE key='alert_wecom_webhook'
          AND value<>''
          AND value NOT LIKE 'enc:v1:%'
        """
    )
    op.execute(
        """
        DO $constraint$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='sys_config'::regclass
              AND conname='ck_sys_config_wecom_ciphertext'
          ) THEN
            ALTER TABLE sys_config
            ADD CONSTRAINT ck_sys_config_wecom_ciphertext
            CHECK (
              key<>'alert_wecom_webhook'
              OR value=''
              OR value LIKE 'enc:v1:%'
            );
          END IF;
        END
        $constraint$
        """
    )


def downgrade() -> None:
    # fail closed：降版本不恢复已清理的 reusable credential，也不允许重新写入明文。
    pass
