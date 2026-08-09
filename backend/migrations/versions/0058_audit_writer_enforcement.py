"""Enforce stable audit subjects and approved system producer/action pairs."""

from __future__ import annotations

from alembic import op

revision = "0058_audit_writer_enforcement"
down_revision = "0057_wecom_credential_encryption"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute(
        r"""
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
  context_app bigint := NULLIF(current_setting('sms.audit_app_id', TRUE),'')::bigint;
  stable_context boolean := FALSE;
  system_allowed boolean := FALSE;
BEGIN
  stable_context := (
    context_actor IS NOT NULL
    AND (
      (
        context_kind='human'
        AND context_account IS NOT NULL
        AND context_identity IS NOT NULL
        AND context_app IS NULL
      )
      OR (
        context_kind='api_app'
        AND context_account IS NULL
        AND context_identity IS NULL
        AND context_app IS NOT NULL
      )
    )
  );

  IF stable_context THEN
    IF NEW.actor_subject_kind<>'legacy_unknown'
       AND (
         NEW.actor_subject_kind IS DISTINCT FROM context_kind
         OR NEW.actor IS DISTINCT FROM context_actor
         OR NEW.actor_account_id IS DISTINCT FROM context_account
         OR NEW.actor_identity_id IS DISTINCT FROM context_identity
         OR NEW.actor_app_id IS DISTINCT FROM context_app
       )
    THEN
      RAISE EXCEPTION 'audit subject does not match authenticated context'
        USING ERRCODE='23514';
    END IF;
    NEW.actor_subject_kind := context_kind;
    NEW.actor := context_actor;
    NEW.actor_account_id := context_account;
    NEW.actor_identity_id := context_identity;
    NEW.actor_app_id := context_app;
    RETURN NEW;
  END IF;

  IF NEW.actor_subject_kind IN ('human','api_app','legacy_unknown') THEN
    RAISE EXCEPTION 'live audit event has no authenticated actor context'
      USING ERRCODE='23514';
  END IF;

  IF NEW.actor_subject_kind='system' THEN
    IF NEW.actor_account_id IS NOT NULL
       OR NEW.actor_identity_id IS NOT NULL
       OR NEW.actor_app_id IS NOT NULL
    THEN
      RAISE EXCEPTION 'system audit event cannot name a stable user or app'
        USING ERRCODE='23514';
    END IF;

    system_allowed := current_user='sms_owner'
      OR (
        current_user='sms_auth'
        AND NEW.actor='auth-system'
        AND NEW.action='account_source_conflict'
      )
      OR (
        current_user='sms_accept'
        AND (
          (
            NEW.actor='vendor-test-reconciler'
            AND NEW.action IN (
              'vendor_test_operation_completed',
              'vendor_test_operation_batch_attached'
            )
          )
          OR (
            NEW.actor='security-report-mailer'
            AND NEW.action='security_daily_delivery_result'
          )
        )
      )
      OR (
        current_user='sms_send'
        AND (
          (
            NEW.actor='vendor-state-sync'
            AND NEW.action IN ('template_sync','sign_sync')
          )
          OR (
            NEW.actor='vendor-test-reconciler'
            AND NEW.action IN (
              'vendor_test_operation_completed',
              'vendor_test_operation_batch_attached'
            )
          )
          OR (
            NEW.actor='security-report-collector'
            AND NEW.action IN (
              'security_daily_generated',
              'security_daily_generation_unavailable'
            )
          )
          OR (
            NEW.actor='security-report-scheduler'
            AND NEW.action IN ('security_daily_send','security_daily_retry')
          )
          OR (
            NEW.actor IN (
              'system:usage-projection',
              'system:usage-projection-auto',
              'operator:usage-projection-cli'
            )
            AND NEW.action='usage_projection_rebuild'
          )
          OR (
            NEW.actor='security-report-mailer'
            AND NEW.action='security_daily_delivery_result'
          )
        )
      )
      OR (
        current_user='sms_scheduler'
        AND (
          (
            NEW.actor='security-report-scheduler'
            AND NEW.action IN ('security_daily_send','security_daily_retry')
          )
          OR (
            NEW.actor='security-report-mailer'
            AND NEW.action='security_daily_delivery_result'
          )
        )
      );

    IF NOT system_allowed THEN
      RAISE EXCEPTION 'system audit producer/action is not authorized for database role'
        USING ERRCODE='23514';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'unsupported live audit subject kind'
    USING ERRCODE='23514';
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
    # fail closed：降版本不恢复显式主体绕过。
    pass
