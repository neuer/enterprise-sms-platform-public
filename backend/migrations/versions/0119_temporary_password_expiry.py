"""临时密码独立期限；历史未改密账号仅迁移时获得一次 24 小时宽限。"""

from alembic import op

revision = "0119_temporary_password_expiry"
down_revision = "0118_role_mapping_invalidation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE local_credential ADD COLUMN IF NOT EXISTS "
        "temporary_password_expires_at TIMESTAMPTZ"
    )
    op.execute("""
        UPDATE local_credential
        SET temporary_password_expires_at = statement_timestamp() + interval '24 hours'
        WHERE must_change_password AND temporary_password_expires_at IS NULL
    """)
    op.execute("""
        DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
          WHERE conrelid='local_credential'::regclass
            AND conname='ck_local_temporary_password_expiry') THEN
        ALTER TABLE local_credential ADD CONSTRAINT ck_local_temporary_password_expiry
        CHECK ((must_change_password AND temporary_password_expires_at IS NOT NULL)
          OR (NOT must_change_password AND temporary_password_expires_at IS NULL));
        END IF; END $$;
    """)
    op.execute("""
        INSERT INTO sys_config(key,value,value_type,description)
        VALUES('local_temporary_password_ttl_hours','24','int',
               '临时密码有效期(小时)，1–168；仅影响新建和重置')
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM local_credential WHERE must_change_password) THEN
          RAISE EXCEPTION 'temporary password downgrade unsafe';
        END IF;
        END $$;
    """)
    op.execute("""
        ALTER TABLE local_credential
        DROP CONSTRAINT IF EXISTS ck_local_temporary_password_expiry,
        DROP COLUMN IF EXISTS temporary_password_expires_at
    """)
    op.execute("DELETE FROM sys_config WHERE key='local_temporary_password_ttl_hours'")
