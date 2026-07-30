"""固化 callback 签名材料并启用上下文绑定密文。"""

from __future__ import annotations

from alembic import op

revision = "0036_callback_crypto_context"
down_revision = "0035_atomic_password_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE callback_task
          ADD COLUMN IF NOT EXISTS callback_secret_enc BYTEA,
          ADD COLUMN IF NOT EXISTS callback_secret_key_version SMALLINT,
          ADD COLUMN IF NOT EXISTS signature_version SMALLINT NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        UPDATE callback_task task
        SET callback_secret_enc=app.callback_secret_enc,
            callback_secret_key_version=(
              (get_byte(app.callback_secret_enc,0) << 8)
              + get_byte(app.callback_secret_enc,1)
            )::smallint
        FROM app
        WHERE app.id=task.app_id
          AND app.callback_secret_enc IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE callback_task
          ALTER COLUMN callback_secret_enc SET NOT NULL,
          ALTER COLUMN callback_secret_key_version SET NOT NULL
        """
    )
    op.execute(
        """
        DO $constraints$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname='chk_callback_secret_key_version'
              AND conrelid='callback_task'::regclass
          ) THEN
            ALTER TABLE callback_task
              ADD CONSTRAINT chk_callback_secret_key_version
              CHECK (callback_secret_key_version > 0);
          END IF;
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname='chk_callback_signature_version'
              AND conrelid='callback_task'::regclass
          ) THEN
            ALTER TABLE callback_task
              ADD CONSTRAINT chk_callback_signature_version
              CHECK (signature_version = 1);
          END IF;
        END
        $constraints$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE callback_task
          DROP CONSTRAINT chk_callback_signature_version,
          DROP CONSTRAINT chk_callback_secret_key_version,
          DROP COLUMN signature_version,
          DROP COLUMN callback_secret_key_version,
          DROP COLUMN callback_secret_enc
        """
    )
