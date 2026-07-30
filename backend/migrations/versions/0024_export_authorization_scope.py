"""以不可枚举 ID、稳定主体和固化部门范围封闭导出任务越权。"""

from __future__ import annotations

from alembic import op

revision = "0024_export_authorization_scope"
down_revision = "0023_vendor_uat_acceptance_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """expand/backfill/contract；无法可靠解析的历史任务保持 fail closed。"""

    # expand
    op.execute(
        """
        ALTER TABLE export_task
          ADD COLUMN IF NOT EXISTS public_id UUID DEFAULT gen_random_uuid(),
          ADD COLUMN IF NOT EXISTS creator_account_id BIGINT,
          ADD COLUMN IF NOT EXISTS scope_dept VARCHAR(128),
          ADD COLUMN IF NOT EXISTS scope_resolved BOOLEAN NOT NULL DEFAULT FALSE
        """
    )

    # backfill: 账号名全局大小写不敏感唯一，只有精确映射到稳定身份时才回填。
    op.execute("UPDATE export_task SET public_id=gen_random_uuid() WHERE public_id IS NULL")
    op.execute(
        """
        UPDATE export_task task
        SET creator_account_id=identity.account_id
        FROM auth_identity identity
        WHERE task.creator_account_id IS NULL
          AND identity.normalized_login_name=lower(btrim(task.creator))
        """
    )
    op.execute(
        """
        UPDATE export_task
        SET scope_dept=filters->>'scope_dept',scope_resolved=TRUE
        WHERE NOT scope_resolved
          AND jsonb_typeof(filters)='object'
          AND filters ? 'scope_dept'
          AND (
            filters->'scope_dept'='null'::jsonb
            OR jsonb_typeof(filters->'scope_dept')='string'
          )
        """
    )

    # contract: public_id 必须存在；历史主体或范围不明时列保持 NULL/FALSE，
    # 应用授权查询强制同时要求两者已解析。
    op.execute("ALTER TABLE export_task ALTER COLUMN public_id SET NOT NULL")
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='export_task_public_id_key'
              AND conrelid='export_task'::regclass
          ) THEN
            ALTER TABLE export_task
              ADD CONSTRAINT export_task_public_id_key UNIQUE(public_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='export_task_creator_account_id_fkey'
              AND conrelid='export_task'::regclass
          ) THEN
            ALTER TABLE export_task
              ADD CONSTRAINT export_task_creator_account_id_fkey
              FOREIGN KEY(creator_account_id)
              REFERENCES user_account(id) ON DELETE RESTRICT
              NOT VALID;
            ALTER TABLE export_task
              VALIDATE CONSTRAINT export_task_creator_account_id_fkey;
          END IF;
        END
        $migration$
        """
    )


def downgrade() -> None:
    """禁止自动恢复可枚举 ID 与用户名宽授权。"""

    raise RuntimeError("export authorization hardening downgrade is intentionally blocked")
