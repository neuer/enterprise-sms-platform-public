"""下发前剔除事实、黑名单代际和部分配额投影。"""

from __future__ import annotations

from alembic import op

revision = "0136_dispatch_exclusion"
down_revision = "0135_resend_supplement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧库补表。schema.sql 已含这些对象时保持同形。"""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_dispatch_exclusion (
            message_id BIGINT NOT NULL,
            message_created_at TIMESTAMPTZ NOT NULL,
            batch_id BIGINT NOT NULL REFERENCES sms_batch(id) ON DELETE RESTRICT,
            reason VARCHAR(32) NOT NULL,
            decision_generation BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (message_id, message_created_at, reason),
            FOREIGN KEY (message_id, message_created_at)
              REFERENCES sms_message(id, created_at) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_exclusion_batch
          ON sms_dispatch_exclusion(batch_id)
        """
    )
    op.execute("REVOKE ALL ON sms_dispatch_exclusion FROM PUBLIC")
    op.execute("GRANT SELECT ON sms_dispatch_exclusion TO sms_accept")
    op.execute("GRANT SELECT, INSERT ON sms_dispatch_exclusion TO sms_send")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS blacklist_policy_generation (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            generation BIGINT NOT NULL CHECK (generation >= 1)
        )
        """
    )
    op.execute(
        """
        INSERT INTO blacklist_policy_generation(id, generation)
        VALUES (1, 1)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute("REVOKE ALL ON blacklist_policy_generation FROM PUBLIC")
    op.execute(
        """
CREATE OR REPLACE FUNCTION bump_blacklist_policy_generation()
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  next_generation bigint;
BEGIN
  UPDATE blacklist_policy_generation
  SET generation = generation + 1
  WHERE id = 1
  RETURNING generation INTO next_generation;
  IF next_generation IS NULL THEN
    RAISE EXCEPTION 'blacklist policy generation unavailable';
  END IF;
  RETURN next_generation;
END
$$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION bump_blacklist_policy_generation() FROM PUBLIC")
    op.execute("GRANT SELECT ON blacklist_policy_generation TO sms_accept")
    op.execute("GRANT SELECT ON blacklist_policy_generation TO sms_send")
    op.execute("GRANT EXECUTE ON FUNCTION bump_blacklist_policy_generation() TO sms_accept")
    op.execute("GRANT EXECUTE ON FUNCTION bump_blacklist_policy_generation() TO sms_send")
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
           AND NEW.action IN (
             'account_source_conflict','auth_account_locked','auth_ip_banned'))
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
          (NEW.action='sensitive_hit' AND NEW.object_type='sms_uncertain_resolution'
           AND NEW.actor ~ '^system_resend:[1-9][0-9]*$'
           AND jsonb_typeof(NEW.after_val->'hit_count')='number'
           AND EXISTS (
             SELECT 1 FROM sms_uncertain_resolution r
             JOIN sms_chunk c ON c.id=r.chunk_id AND c.batch_id=r.batch_id
             JOIN sms_batch b ON b.id=r.batch_id
             JOIN user_account proposer ON proposer.id=r.proposer_account_id
             JOIN user_account confirmer ON confirmer.id=r.confirmer_account_id
             WHERE NEW.actor='system_resend:' || r.id::text
               AND NEW.object_id=r.id::text
               AND NEW.after_val->>'effect_generation'=r.effect_generation::text
               AND r.action='resend_new_batch'
               AND r.state IN ('approved','effect_pending','applying','retryable_effect_error')
               AND r.proposer_account_id<>r.confirmer_account_id
               AND proposer.status=1 AND proposer.role='admin'
               AND confirmer.status=1 AND confirmer.role='admin'
               AND c.status='unknown_terminal'
               AND b.channel=r.source_channel AND b.category=r.source_category
               AND b.dept=r.source_dept
               AND b.app_id IS NOT DISTINCT FROM r.source_app_id
           ))
          OR
          (NEW.action='message_send' AND NEW.object_type='batch'
           AND NEW.actor ~ '^system_resend:[1-9][0-9]*$'
           AND NEW.after_val @> '{"uncertain_resend": true}'::jsonb
           AND EXISTS (
             SELECT 1 FROM sms_uncertain_resolution r
             JOIN sms_uncertain_child uc ON uc.resolution_id=r.id
             JOIN sms_batch child ON child.id=uc.child_batch_id
             WHERE NEW.actor='system_resend:' || r.id::text
               AND NEW.object_id=trim(child.batch_no)
               AND uc.provenance_verified AND r.child_batch_id=child.id
               AND uc.generation=r.effect_generation
               AND NEW.after_val->>'effect_generation'=r.effect_generation::text
               AND r.action='resend_new_batch'
               AND r.state IN ('approved','effect_pending','applying','retryable_effect_error')
               AND r.proposer_account_id<>r.confirmer_account_id
           ))
          OR
          (NEW.actor='vendor-state-sync'
           AND NEW.action IN ('template_sync','sign_sync','sign_adopt'))
          OR (NEW.actor='vendor-test-reconciler' AND NEW.action IN (
            'vendor_test_operation_completed','vendor_test_operation_batch_attached'))
          OR (NEW.actor='system-reconcile' AND NEW.action='raw_replay')
          OR (NEW.actor IN ('system:usage-projection','system:usage-projection-auto')
              AND NEW.action='usage_projection_rebuild')
          OR (NEW.actor='dispatch-blacklist' AND NEW.action='dispatch_excluded')))
      OR (context_domain='bulk' AND session_user='sms_send' AND (
          (NEW.actor='security-report-collector' AND NEW.action IN (
            'security_daily_generated','security_daily_generation_unavailable'))
          OR (NEW.actor='import-parser' AND NEW.action='message_import')
          OR (NEW.actor='security-report-scheduler'
              AND NEW.action IN ('security_daily_send','security_daily_retry'))
          OR (NEW.actor='security-report-mailer'
              AND NEW.action='security_daily_delivery_result')
          OR (NEW.actor IN ('system:usage-projection','system:usage-projection-auto')
              AND NEW.action='usage_projection_rebuild')
          OR (NEW.actor='dispatch-blacklist' AND NEW.action='dispatch_excluded')));

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
$$;
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_task_name")
    op.execute(
        """
        ALTER TABLE outbox_event
          ADD CONSTRAINT ck_outbox_task_name
          CHECK (task_name IN (
                     'app.tasks.bind_sign',
                     'app.tasks.bind_template',
                     'app.tasks.adopt_sign',
                     'app.tasks.sync_template',
                     'app.tasks.send.process_batch',
                     'app.tasks.send.process_chunk',
                     'app.tasks.deliver_callback',
                     'app.tasks.outbox.compensate_quota',
                     'app.tasks.outbox.deliver_alert',
                     'app.tasks.outbox.release_usage',
                     'app.tasks.outbox.publish_usage_projection',
                     'app.tasks.outbox.trigger_job',
                     'app.tasks.outbox.apply_uncertain_effect'
                   ))
        """
    )
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_args_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event
          ADD CONSTRAINT ck_outbox_args_no_pii
              CHECK (
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
              task_name IN (
                'app.tasks.send.process_chunk',
                'app.tasks.outbox.apply_uncertain_effect'
              )
              AND jsonb_array_length(args)=1
              AND jsonb_typeof(args->0)='number'
              AND args->>0 ~ '^[1-9][0-9]*$'
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
        OR (
          task_name='app.tasks.outbox.publish_usage_projection'
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
    op.execute("ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS ck_outbox_refs_no_pii")
    op.execute(
        """
        ALTER TABLE outbox_event
          ADD CONSTRAINT ck_outbox_refs_no_pii
              CHECK (
      (
        aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
        OR (
          task_name IN (
            'app.tasks.bind_sign',
            'app.tasks.bind_template',
            'app.tasks.adopt_sign',
            'app.tasks.sync_template',
            'app.tasks.outbox.apply_uncertain_effect'
          )
          AND aggregate_id ~ '^[1-9][0-9]*$'
        )
        OR (
          task_name='app.tasks.send.process_batch'
          AND aggregate_id ~ '^[0-9a-f]{32}$'
        )
        OR (
          task_name='app.tasks.send.process_chunk'
          AND aggregate_id ~ '^[1-9][0-9]*$'
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
        OR (
          task_name='app.tasks.outbox.publish_usage_projection'
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
              || 'chunk[.]ready[:][1-9][0-9]*|'
              || 'uncertain[.]effect[:][1-9][0-9]*[:][1-9][0-9]*|'
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
          || '[0-9a-f]{12}|'
          || 'usage[.]projection[:][0-9a-f]{8}-[0-9a-f]{4}-'
          || '[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-'
          || '[0-9a-f]{12}[:][1-9][0-9]*[:][1-9][0-9]*)$'
        )
      )
    )
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS bump_blacklist_policy_generation()")
    op.execute("DROP TABLE IF EXISTS blacklist_policy_generation")
    op.execute("DROP TABLE IF EXISTS sms_dispatch_exclusion")
