"""统一安全版本并由数据库触发器覆盖所有授权上下文写路径。"""

from __future__ import annotations

from alembic import op

revision = "0025_security_session_projection"
down_revision = "0024_export_authorization_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """双列兼容旧 writer，并建立覆盖全部授权上下文的失效触发器。"""

    op.execute(
        """
        ALTER TABLE user_account
          ADD COLUMN IF NOT EXISTS auth_version BIGINT,
          ADD COLUMN IF NOT EXISTS security_version BIGINT
        """
    )
    op.execute(
        """
        UPDATE user_account
        SET auth_version=COALESCE(auth_version,security_version,1),
            security_version=COALESCE(security_version,auth_version,1)
        WHERE auth_version IS NULL OR security_version IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE user_account
          ALTER COLUMN auth_version SET DEFAULT 1,
          ALTER COLUMN auth_version SET NOT NULL,
          ALTER COLUMN security_version SET DEFAULT 1,
          ALTER COLUMN security_version SET NOT NULL
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='user_account_auth_version_check'
              AND conrelid='user_account'::regclass
          ) THEN
            ALTER TABLE user_account
              ADD CONSTRAINT user_account_auth_version_check
              CHECK (auth_version>0);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='user_account_security_version_check'
              AND conrelid='user_account'::regclass
          ) THEN
            ALTER TABLE user_account
              ADD CONSTRAINT user_account_security_version_check
              CHECK (security_version>0);
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bump_account_security_version()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.dept IS DISTINCT FROM OLD.dept
             OR NEW.role IS DISTINCT FROM OLD.role
             OR NEW.role_override IS DISTINCT FROM OLD.role_override
             OR NEW.status IS DISTINCT FROM OLD.status THEN
            IF NEW.security_version = OLD.security_version THEN
              NEW.security_version := OLD.security_version + 1;
            END IF;
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_user_account_security_version ON user_account"
    )
    op.execute(
        """
        CREATE TRIGGER trg_user_account_security_version
        BEFORE UPDATE ON user_account
        FOR EACH ROW EXECUTE FUNCTION bump_account_security_version()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_account_security_versions()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.security_version IS DISTINCT FROM OLD.security_version
             AND NEW.auth_version IS DISTINCT FROM OLD.auth_version
             AND NEW.security_version IS DISTINCT FROM NEW.auth_version THEN
            RAISE EXCEPTION 'account security versions diverged';
          ELSIF NEW.security_version IS DISTINCT FROM OLD.security_version THEN
            NEW.auth_version := NEW.security_version;
          ELSIF NEW.auth_version IS DISTINCT FROM OLD.auth_version THEN
            NEW.security_version := NEW.auth_version;
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS zz_trg_account_security_version_sync ON user_account"
    )
    op.execute(
        """
        CREATE TRIGGER zz_trg_account_security_version_sync
        BEFORE UPDATE ON user_account
        FOR EACH ROW EXECUTE FUNCTION sync_account_security_versions()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bump_identity_security_version()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.account_id IS DISTINCT FROM OLD.account_id
             OR NEW.provider_id IS DISTINCT FROM OLD.provider_id
             OR NEW.login_name IS DISTINCT FROM OLD.login_name
             OR NEW.external_subject IS DISTINCT FROM OLD.external_subject
             OR NEW.status IS DISTINCT FROM OLD.status
             OR NEW.source_groups IS DISTINCT FROM OLD.source_groups THEN
            UPDATE user_account
            SET security_version=security_version+1,updated_at=now()
            WHERE id IN (OLD.account_id,NEW.account_id);
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_auth_identity_security_version ON auth_identity"
    )
    op.execute(
        """
        CREATE TRIGGER trg_auth_identity_security_version
        AFTER UPDATE ON auth_identity
        FOR EACH ROW EXECUTE FUNCTION bump_identity_security_version()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bump_provider_security_version()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.enabled IS DISTINCT FROM OLD.enabled
             OR NEW.active_version IS DISTINCT FROM OLD.active_version
             OR NEW.active_config IS DISTINCT FROM OLD.active_config THEN
            UPDATE user_account ua
            SET security_version=ua.security_version+1,updated_at=now()
            FROM auth_identity ai
            WHERE ai.account_id=ua.id AND ai.provider_id=NEW.id;
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_auth_provider_security_version ON auth_provider"
    )
    op.execute(
        """
        CREATE TRIGGER trg_auth_provider_security_version
        AFTER UPDATE ON auth_provider
        FOR EACH ROW EXECUTE FUNCTION bump_provider_security_version()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bump_role_mapping_security_version()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          affected_provider BIGINT;
        BEGIN
          IF TG_OP='UPDATE' THEN
            UPDATE user_account ua
            SET security_version=ua.security_version+1,updated_at=now()
            FROM auth_identity ai
            WHERE ai.account_id=ua.id
              AND ai.provider_id IN (OLD.provider_id,NEW.provider_id);
            RETURN NEW;
          END IF;
          affected_provider := CASE
            WHEN TG_OP='DELETE' THEN OLD.provider_id
            ELSE NEW.provider_id
          END;
          UPDATE user_account ua
          SET security_version=ua.security_version+1,updated_at=now()
          FROM auth_identity ai
          WHERE ai.account_id=ua.id AND ai.provider_id=affected_provider;
          RETURN COALESCE(NEW,OLD);
        END
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_external_role_mapping_security_version
          ON external_role_mapping
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_external_role_mapping_security_version
        AFTER INSERT OR UPDATE OR DELETE ON external_role_mapping
        FOR EACH ROW EXECUTE FUNCTION bump_role_mapping_security_version()
        """
    )


def downgrade() -> None:
    """禁止恢复只校验账号状态、且没有双列同步的旧语义。"""

    raise RuntimeError("security session projection downgrade is intentionally blocked")
