"""Revoke queued callbacks when their application authority changes."""

from __future__ import annotations

from alembic import op

revision = "0055_callback_revocation"
down_revision = "0054_sensitive_config_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION revoke_callback_tasks_on_app_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
          IF OLD.callback_url IS DISTINCT FROM NEW.callback_url
             OR OLD.callback_secret_enc IS DISTINCT FROM NEW.callback_secret_enc
             OR (
               OLD.callback_report_enabled=true
               AND NEW.callback_report_enabled=false
             )
             OR (OLD.status=1 AND NEW.status<>1)
          THEN
            UPDATE public.callback_task SET status='dead',retry_count=0,
              next_retry_at=NULL,lease_id=NULL,lease_expires_at=NULL,
              last_http_code=NULL,last_error='CallbackConfigRevoked',
              finished_at=now()
            WHERE app_id=NEW.id AND status IN ('pending','retrying')
              AND (
                OLD.callback_url IS DISTINCT FROM NEW.callback_url
                OR OLD.callback_secret_enc IS DISTINCT FROM NEW.callback_secret_enc
                OR (OLD.status=1 AND NEW.status<>1)
                OR (
                  OLD.callback_report_enabled=true
                  AND NEW.callback_report_enabled=false
                  AND event='message.report'
                )
              );
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION revoke_callback_tasks_on_app_change() FROM PUBLIC"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_app_revoke_callback_tasks ON app")
    op.execute(
        """
        CREATE TRIGGER trg_app_revoke_callback_tasks
        AFTER UPDATE OF callback_url,callback_secret_enc,
          callback_report_enabled,status ON app
        FOR EACH ROW EXECUTE FUNCTION revoke_callback_tasks_on_app_change()
        """
    )


def downgrade() -> None:
    # fail closed：降版本只移动 Alembic revision，不移除撤销触发器。
    pass
