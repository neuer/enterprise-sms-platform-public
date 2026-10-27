"""幂等指纹把省略签名和当时的默认签名分开。"""

from __future__ import annotations

from alembic import op

revision = "0133_idempotency_sign_source"
down_revision = "0132_approval_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧记录保持格式 1；schema.sql 基线已含列时幂等跳过。"""

    op.execute(
        """
        ALTER TABLE idempotency_record
          ADD COLUMN IF NOT EXISTS fingerprint_format_version SMALLINT NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE idempotency_claim
          ADD COLUMN IF NOT EXISTS fingerprint_format_version SMALLINT NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'idempotency_record'::regclass
              AND conname = 'ck_idem_fingerprint_format'
          ) THEN
            ALTER TABLE idempotency_record
              ADD CONSTRAINT ck_idem_fingerprint_format
              CHECK (fingerprint_format_version IN (1, 2));
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'idempotency_claim'::regclass
              AND conname = 'ck_idempotency_claim_fingerprint_format'
          ) THEN
            ALTER TABLE idempotency_claim
              ADD CONSTRAINT ck_idempotency_claim_fingerprint_format
              CHECK (fingerprint_format_version IN (1, 2));
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    """只供本地回滚演练；生产不执行降级。"""

    op.execute(
        "ALTER TABLE idempotency_record DROP CONSTRAINT IF EXISTS ck_idem_fingerprint_format"
    )
    op.execute(
        "ALTER TABLE idempotency_claim "
        "DROP CONSTRAINT IF EXISTS ck_idempotency_claim_fingerprint_format"
    )
    op.execute("ALTER TABLE idempotency_record DROP COLUMN IF EXISTS fingerprint_format_version")
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS fingerprint_format_version")
