"""来源准入阈值及单调更新围栏；复用既有 sys_config 权限与审计。"""

from __future__ import annotations

from alembic import op

revision = "0112_auth_admission_policy"
down_revision = "0111_report_batch_active_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO sys_config (key,value,value_type,description)
        VALUES ('auth_admission_policy',
          '{"version": 1,"shared_burst": 100,"shared_window": 200,"shared_refill_ms": 1000,
            "global_burst": 8,"global_refill_ms": 250,"global_concurrent": 4,
            "source_concurrent": 2}',
          'json','登录来源准入阈值；可信出口由部署文件批准')
        ON CONFLICT (key) DO NOTHING
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION advance_auth_admission_revision()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        BEGIN
          IF NEW.key = 'auth_admission_policy' THEN
            NEW.updated_at := GREATEST(
              clock_timestamp(), OLD.updated_at + interval '1 microsecond'
            );
          END IF;
          RETURN NEW;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION advance_auth_admission_revision() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_auth_admission_revision ON sys_config")
    op.execute("""
        CREATE TRIGGER trg_auth_admission_revision
        BEFORE UPDATE ON sys_config FOR EACH ROW
        EXECUTE FUNCTION advance_auth_admission_revision()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_auth_admission_revision ON sys_config")
    op.execute("DROP FUNCTION IF EXISTS advance_auth_admission_revision()")
    op.execute("DELETE FROM sys_config WHERE key = 'auth_admission_policy'")
