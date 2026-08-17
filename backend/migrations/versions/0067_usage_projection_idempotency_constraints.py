"""Add unique projection version and app-scope idempotency CHECK."""

from __future__ import annotations

from alembic import op

revision = "0067_usage_projection_idempotency_constraints"
down_revision = "0066_security_findings_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线（0001）已包含这些约束；真实 0066 旧库则尚未包含。
    # 因此后续迁移必须幂等，才能同时支持当前空库建库与存量升级。
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='usage_projection'::regclass
              AND conname='uk_usage_projection_version'
          ) THEN
            ALTER TABLE usage_projection
              ADD CONSTRAINT uk_usage_projection_version UNIQUE (version);
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='idempotency_record'::regclass
              AND conname='ck_idem_app_scope'
          ) THEN
            ALTER TABLE idempotency_record
              ADD CONSTRAINT ck_idem_app_scope
              CHECK (
                scope_kind <> 'app'
                OR (app_id IS NOT NULL AND scope_id = app_id::text)
              );
          END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE idempotency_record "
        "DROP CONSTRAINT IF EXISTS ck_idem_app_scope"
    )
    op.execute(
        "ALTER TABLE usage_projection "
        "DROP CONSTRAINT IF EXISTS uk_usage_projection_version"
    )
