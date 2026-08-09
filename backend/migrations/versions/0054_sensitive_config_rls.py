"""Restrict reusable notification credentials by runtime database role."""

from __future__ import annotations

from alembic import op

revision = "0054_sensitive_config_rls"
down_revision = "0053_idempotency_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("REVOKE UPDATE ON export_task FROM sms_export")
    op.execute(
        "GRANT UPDATE ("
        "status,file_path,row_count,started_at,lease_id,lease_expires_at,"
        "takeover_count,finished_at"
        ") ON export_task TO sms_export"
    )
    op.execute("ALTER TABLE sys_config ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $policy$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_policy
            WHERE polrelid='sys_config'::regclass
              AND polname='sys_config_accept_all'
          ) THEN
            CREATE POLICY sys_config_accept_all ON sys_config
              FOR ALL TO sms_accept USING (true) WITH CHECK (true);
          END IF;
        END
        $policy$
        """
    )
    op.execute(
        """
        DO $policy$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_policy
            WHERE polrelid='sys_config'::regclass
              AND polname='sys_config_callback_select'
          ) THEN
            CREATE POLICY sys_config_callback_select ON sys_config
              FOR SELECT TO sms_callback
              USING (key <> 'security_daily_resend_api_key');
          END IF;
        END
        $policy$
        """
    )
    op.execute(
        """
        DO $policy$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_policy
            WHERE polrelid='sys_config'::regclass
              AND polname='sys_config_nonsecret_select'
          ) THEN
            CREATE POLICY sys_config_nonsecret_select ON sys_config
              FOR SELECT TO sms_auth, sms_send, sms_export, sms_scheduler
              USING (
                key NOT IN (
                  'alert_wecom_webhook','security_daily_resend_api_key'
                )
              );
          END IF;
        END
        $policy$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION alert_channel_availability()
        RETURNS TABLE(wecom_configured boolean, smtp_configured boolean)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT
            EXISTS(
              SELECT 1 FROM public.sys_config
              WHERE key='alert_wecom_webhook' AND btrim(value)<>''
            ),
            EXISTS(
              SELECT 1 FROM public.sys_config
              WHERE key='alert_mail_to' AND btrim(value)<>''
            )
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION alert_channel_availability() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION alert_channel_availability() TO "
        "sms_auth, sms_accept, sms_send, sms_callback, sms_export, sms_scheduler"
    )


def downgrade() -> None:
    # fail closed：降版本只移动 Alembic revision，不移除 RLS、policy 或安全投影函数。
    pass
