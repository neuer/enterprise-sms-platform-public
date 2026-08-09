"""Bind live audit rows to the authenticated stable principal and request correlation."""

from __future__ import annotations

from alembic import op

revision = "0056_audit_attribution_context"
down_revision = "0055_callback_revocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE audit_log ALTER COLUMN correlation_id SET DEFAULT
          COALESCE(
            NULLIF(current_setting('sms.correlation_id', TRUE),'')::uuid,
            gen_random_uuid()
          )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_live_audit_principal()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
          context_kind text := NULLIF(current_setting('sms.audit_subject_kind', TRUE),'');
          context_actor text := NULLIF(current_setting('sms.audit_actor_name', TRUE),'');
          context_account bigint := NULLIF(
            current_setting('sms.audit_account_id', TRUE),''
          )::bigint;
          context_identity bigint := NULLIF(
            current_setting('sms.audit_identity_id', TRUE),''
          )::bigint;
          context_app bigint := NULLIF(
            current_setting('sms.audit_app_id', TRUE),''
          )::bigint;
        BEGIN
          IF NEW.actor_subject_kind='legacy_unknown' THEN
            IF context_actor IS NULL THEN
              RAISE EXCEPTION 'live audit event has no authenticated actor context'
                USING ERRCODE='23514';
            END IF;
            IF context_kind='human'
               AND context_account IS NOT NULL
               AND context_identity IS NOT NULL
               AND context_app IS NULL
            THEN
              NEW.actor_subject_kind := 'human';
              NEW.actor := context_actor;
              NEW.actor_account_id := context_account;
              NEW.actor_identity_id := context_identity;
              NEW.actor_app_id := NULL;
            ELSIF context_kind='api_app'
                  AND context_account IS NULL
                  AND context_identity IS NULL
                  AND context_app IS NOT NULL
            THEN
              NEW.actor_subject_kind := 'api_app';
              NEW.actor := context_actor;
              NEW.actor_account_id := NULL;
              NEW.actor_identity_id := NULL;
              NEW.actor_app_id := context_app;
            ELSE
              RAISE EXCEPTION 'live audit event has incomplete stable actor context'
                USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION enforce_live_audit_principal() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_require_live_principal ON audit_log")
    op.execute(
        """
        CREATE TRIGGER trg_audit_require_live_principal
        BEFORE INSERT ON audit_log
        FOR EACH ROW EXECUTE FUNCTION enforce_live_audit_principal()
        """
    )


def downgrade() -> None:
    # fail closed：降版本只移动 Alembic revision，不恢复可写入 legacy_unknown 的路径。
    pass
