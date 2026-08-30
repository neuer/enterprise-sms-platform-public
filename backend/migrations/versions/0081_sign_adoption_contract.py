"""允许精确关联已有厂商签名的 Outbox 与系统审计。"""

from __future__ import annotations

from alembic import op

revision = "0081_sign_adoption_contract"
down_revision = "0080_security_daily_delivery_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_task_name"
    )
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_args_no_pii"
    )
    op.execute(
        "ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_refs_no_pii"
    )
    op.execute(
        r"""
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_task_name CHECK (task_name IN (
          'app.tasks.bind_sign',
          'app.tasks.bind_template',
          'app.tasks.adopt_sign',
          'app.tasks.sync_template',
          'app.tasks.send.process_batch',
          'app.tasks.deliver_callback',
          'app.tasks.outbox.compensate_quota',
          'app.tasks.outbox.deliver_alert',
          'app.tasks.outbox.release_usage',
          'app.tasks.outbox.trigger_job'
        ))
        """
    )
    op.execute(
        r"""
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_args_no_pii CHECK (
          (
            args::text !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name IN (
                'app.tasks.bind_sign',
                'app.tasks.bind_template',
                'app.tasks.sync_template'
              )
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[1-9][0-9]*$'
            )
            OR (
              task_name='app.tasks.adopt_sign'
              AND jsonb_array_length(args)=2
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[1-9][0-9]*$'
              AND jsonb_typeof(args->1)='number'
              AND args->>1 ~ '^[1-9][0-9]*$'
              AND (args->>1)::bigint <= 2147483647
            )
            OR (
              task_name='app.tasks.send.process_batch'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.deliver_callback'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.deliver_alert'
              AND jsonb_array_length(args)=2
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND args->>1 IN ('wecom','smtp')
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND jsonb_array_length(args)=6
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[0-9]+$'
              AND jsonb_typeof(args->1)='string'
              AND args->>1 !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
              AND args->>2 IN ('verify','notice','market')
              AND args->>3 ~ '^[0-9]{8}$'
              AND jsonb_typeof(args->4)='number'
              AND args->>4 ~ '^[0-9]+$'
              AND args->>5 ~ (
                '^(batch[:][0-9a-f]{32}[:]cancelled|'
                || 'approval[:][1-9][0-9]*[:](rejected|expired))$'
              )
            )
            OR (
              task_name='app.tasks.outbox.release_usage'
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='string'
              AND args->>0 ~ (
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
                || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              )
            )
          )
          AND args::text !~ (
            '"(phone|phones|mobile|mobiles|phone_enc|phone_hmac|'
            || 'content|body|secret|password)"[[:space:]]*:'
          )
        )
        """
    )
    op.execute(
        r"""
        ALTER TABLE outbox_event
        ADD CONSTRAINT ck_outbox_refs_no_pii CHECK (
          (
            aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR (
              task_name IN (
                'app.tasks.bind_sign',
                'app.tasks.bind_template',
                'app.tasks.adopt_sign',
                'app.tasks.sync_template'
              )
              AND aggregate_id ~ '^[1-9][0-9]*$'
            )
            OR (
              task_name='app.tasks.send.process_batch'
              AND aggregate_id ~ '^[0-9a-f]{32}$'
            )
            OR (
              task_name='app.tasks.outbox.compensate_quota'
              AND aggregate_id ~ '^([0-9a-f]{32}|[0-9]+)$'
            )
            OR (
              task_name IN (
                'app.tasks.deliver_callback',
                'app.tasks.outbox.deliver_alert'
              )
              AND aggregate_id ~ '^[0-9]+$'
            )
            OR (
              task_name='app.tasks.outbox.release_usage'
              AND aggregate_id ~ (
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
                || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              )
            )
          )
          AND (
            dedup_key !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
            OR dedup_key ~ (
              '^(batch[.]ready[:][0-9a-f]{32}|'
              || 'scheduled[:][0-9a-f]{32}[:]ready|'
              || 'batch[:][0-9a-f]{32}[:]cancelled|'
              || 'approval[:][1-9][0-9]*[:](approved|rejected|expired)|'
              || 'callback[:][1-9][0-9]*[:]attempt[:][0-9]+|'
              || 'alert[:][1-9][0-9]*[:](wecom|smtp)|'
              || 'template[.]sync[:][1-9][0-9]*[:][1-9][0-9]*|'
              || '(template[.]bind|sign[.]bind|sign[.]adopt)[:][1-9][0-9]*[:]'
              || '[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              || '[89ab][0-9a-f]{3}-[0-9a-f]{12}|'
              || 'usage[.]release[:][0-9a-f]{8}-[0-9a-f]{4}-'
              || '[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-'
              || '[0-9a-f]{12})$'
            )
          )
        )
        """
    )
    op.execute(
        r"""
CREATE OR REPLACE FUNCTION enforce_live_audit_principal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
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
  context_correlation text := NULLIF(
    current_setting('sms.correlation_id', TRUE),''
  );
  context_signature text := NULLIF(
    current_setting('sms.audit_context_signature', TRUE),''
  );
  context_action text := NULLIF(current_setting('sms.audit_action', TRUE),'');
  context_domain text := NULLIF(
    current_setting('sms.audit_producer_domain', TRUE),''
  );
  audit_key bytea;
  expected_signature text;
  signature_payload text;
  stable_context boolean := FALSE;
  system_allowed boolean := FALSE;
BEGIN
  stable_context := (
    context_actor IS NOT NULL
    AND context_correlation IS NOT NULL
    AND (
      (context_kind='human' AND context_account IS NOT NULL
       AND context_identity IS NOT NULL AND context_app IS NULL)
      OR
      (context_kind='api_app' AND context_account IS NULL
       AND context_identity IS NULL AND context_app IS NOT NULL)
    )
  );

  IF stable_context THEN
    IF session_user<>'sms_owner' THEN
      SELECT key_material INTO audit_key
      FROM public.audit_context_signing_key WHERE key_kind='principal';
      IF audit_key IS NULL THEN
        RAISE EXCEPTION 'audit context signing key is unavailable'
          USING ERRCODE='23514';
      END IF;
      signature_payload := concat_ws(E'\n',
        'v2',txid_current()::text,
        encode(convert_to(session_user,'UTF8'),'hex'),
        encode(convert_to(context_correlation,'UTF8'),'hex'),
        encode(convert_to(context_kind,'UTF8'),'hex'),
        encode(convert_to(context_actor,'UTF8'),'hex'),
        coalesce(context_account::text,''),
        coalesce(context_identity::text,''),
        coalesce(context_app::text,'')
      );
      expected_signature := encode(
        public.hmac(convert_to(signature_payload,'UTF8'),audit_key,'sha256'),
        'hex'
      );
      IF context_signature IS NULL
         OR length(context_signature)<>64
         OR context_signature IS DISTINCT FROM expected_signature
      THEN
        RAISE EXCEPTION 'audit context signature is invalid'
          USING ERRCODE='23514';
      END IF;
    END IF;
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
    NEW.correlation_id := context_correlation::uuid;
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
    IF NEW.actor_account_id IS NOT NULL OR NEW.actor_identity_id IS NOT NULL
       OR NEW.actor_app_id IS NOT NULL
    THEN
      RAISE EXCEPTION 'system audit event cannot name a stable user or app'
        USING ERRCODE='23514';
    END IF;

    IF context_kind IS DISTINCT FROM 'system'
       OR context_actor IS DISTINCT FROM NEW.actor
       OR context_action IS DISTINCT FROM NEW.action
       OR context_domain NOT IN ('api','realtime','bulk')
       OR context_correlation IS NULL
    THEN
      RAISE EXCEPTION 'system audit event has no authenticated producer context'
        USING ERRCODE='23514';
    END IF;

    IF session_user<>'sms_owner' THEN
      SELECT key_material INTO audit_key
      FROM public.audit_context_signing_key
      WHERE key_kind='system:' || context_domain;
      IF audit_key IS NULL THEN
        RAISE EXCEPTION 'system audit signing key is unavailable'
          USING ERRCODE='23514';
      END IF;
      signature_payload := concat_ws(E'\n',
        'system-v2',txid_current()::text,
        encode(convert_to(session_user,'UTF8'),'hex'),
        encode(convert_to(context_correlation,'UTF8'),'hex'),
        encode(convert_to(context_domain,'UTF8'),'hex'),
        encode(convert_to(context_actor,'UTF8'),'hex'),
        encode(convert_to(context_action,'UTF8'),'hex')
      );
      expected_signature := encode(
        public.hmac(convert_to(signature_payload,'UTF8'),audit_key,'sha256'),
        'hex'
      );
      IF context_signature IS NULL
         OR length(context_signature)<>64
         OR context_signature IS DISTINCT FROM expected_signature
      THEN
        RAISE EXCEPTION 'system audit context signature is invalid'
          USING ERRCODE='23514';
      END IF;
    END IF;

    system_allowed := session_user='sms_owner'
      OR (context_domain='api' AND (
          (session_user='sms_auth' AND NEW.actor='auth-system'
           AND NEW.action='account_source_conflict')
          OR (session_user='sms_accept' AND (
            (NEW.actor='vendor-test-reconciler' AND NEW.action IN (
              'vendor_test_operation_completed','vendor_test_operation_batch_attached'))
            OR (NEW.actor='security-report-collector' AND NEW.action IN (
              'security_daily_generated','security_daily_generation_unavailable'))
            OR (NEW.actor='security-report-mailer'
                AND NEW.action='security_daily_delivery_result')
            OR (NEW.actor IN (
                  'system:usage-projection','system:usage-projection-auto',
                  'operator:usage-projection-cli')
                AND NEW.action='usage_projection_rebuild')))))
      OR (context_domain='realtime' AND session_user='sms_send' AND (
          (NEW.actor='vendor-state-sync'
           AND NEW.action IN ('template_sync','sign_sync','sign_adopt'))
          OR (NEW.actor='vendor-test-reconciler' AND NEW.action IN (
            'vendor_test_operation_completed','vendor_test_operation_batch_attached'))
          OR (NEW.actor='system-reconcile' AND NEW.action='raw_replay')
          OR (NEW.actor IN ('system:usage-projection','system:usage-projection-auto')
              AND NEW.action='usage_projection_rebuild')))
      OR (context_domain='bulk' AND session_user='sms_send' AND (
          (NEW.actor='security-report-collector' AND NEW.action IN (
            'security_daily_generated','security_daily_generation_unavailable'))
          OR (NEW.actor='import-parser' AND NEW.action='message_import')
          OR (NEW.actor='security-report-scheduler'
              AND NEW.action IN ('security_daily_send','security_daily_retry'))
          OR (NEW.actor='security-report-mailer'
              AND NEW.action='security_daily_delivery_result')
          OR (NEW.actor IN ('system:usage-projection','system:usage-projection-auto')
              AND NEW.action='usage_projection_rebuild')));

    IF NOT system_allowed THEN
      RAISE EXCEPTION 'system audit producer/action is not authorized for database role'
        USING ERRCODE='23514';
    END IF;
    NEW.correlation_id := context_correlation::uuid;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'unsupported live audit subject kind'
    USING ERRCODE='23514';
END
$$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION enforce_live_audit_principal() FROM PUBLIC")


def downgrade() -> None:
    # 生产 schema 不回退，避免已合法写入的关联审计在旧白名单下失效。
    pass
