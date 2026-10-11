"""授权锁与业务、审计共用事务；业务角色仅执行受限函数。"""

from alembic import op

revision = "0124_business_authorization_lock"
down_revision = "0123_send_db_failure_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION lock_business_authorization(
            p_account bigint, p_identity bigint, p_version bigint, p_provider text,
            p_mode text, p_role text, p_dept text, p_login text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            matched bigint;
        BEGIN
            IF p_mode IS NULL OR p_mode NOT IN ('admin','export')
               OR (p_mode='admin' AND p_role IS DISTINCT FROM 'admin') THEN
                RETURN FALSE;
            END IF;
            PERFORM pg_advisory_xact_lock(783692261159983410::bigint);
            SELECT ua.id INTO matched
              FROM public.user_account ua
              JOIN public.auth_identity ai ON ai.account_id=ua.id
              JOIN public.auth_provider ap ON ap.id=ai.provider_id
              WHERE ua.id=p_account AND ai.id=p_identity
                AND ua.security_version=p_version AND ap.code=p_provider
                AND ua.status=1 AND ai.status=1 AND ap.enabled=TRUE
                AND ua.role=p_role
                AND (p_mode='admin' OR (ua.dept=p_dept AND ai.login_name=p_login))
                AND (p_mode='export' OR (
                  (ap.kind='local' OR (
                    SELECT count(DISTINCT btrim(erm.dept))
                    FROM public.external_role_mapping erm
                    WHERE erm.provider_id=ap.id AND erm.external_group=ANY(ai.source_groups)
                      AND NULLIF(btrim(erm.dept),'') IS NOT NULL
                  )=1)
                  AND (ua.role_override=TRUE OR EXISTS (
                    SELECT 1 FROM public.external_role_mapping erm
                    WHERE erm.provider_id=ap.id AND erm.external_group=ANY(ai.source_groups)
                      AND erm.role='admin'
                  ))
                ))
              FOR SHARE OF ua,ai,ap;
            RETURN FOUND;
        END
        $$;
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION lock_business_authorization(
            bigint,bigint,bigint,text,text,text,text,text
        ) FROM PUBLIC;
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION lock_business_authorization(
            bigint,bigint,bigint,text,text,text,text,text
        ) TO sms_accept,sms_export;
    """)


def downgrade() -> None:
    # 保留只读授权能力以支持应用镜像回退，不放宽现有角色权限。
    pass
