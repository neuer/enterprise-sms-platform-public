"""为授权、所有权、职责分离和审计增加不可变主体 ID。"""

from __future__ import annotations

from alembic import op

revision = "0026_stable_principal_ids"
down_revision = "0025_security_session_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Expand 后只回填已有稳定证据；用户名快照绝不用于猜测绑定。"""

    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='uq_auth_identity_id_account'
              AND conrelid='auth_identity'::regclass
          ) THEN
            ALTER TABLE auth_identity
              ADD CONSTRAINT uq_auth_identity_id_account UNIQUE (id,account_id);
          END IF;
        END
        $migration$
        """
    )
    # 保持 SQL 为静态字面量，使远端 expand-only 检查器可逐句证明。
    op.execute(
        """
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS creator_account_id BIGINT
            REFERENCES user_account(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS creator_identity_id BIGINT
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_sms_batch_creator_identity'
              AND conrelid='sms_batch'::regclass
          ) THEN
            ALTER TABLE sms_batch
              ADD CONSTRAINT fk_sms_batch_creator_identity
              FOREIGN KEY (creator_identity_id,creator_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_sms_batch_creator_principal_pair'
              AND conrelid='sms_batch'::regclass
          ) THEN
            ALTER TABLE sms_batch
              ADD CONSTRAINT ck_sms_batch_creator_principal_pair CHECK (
                (creator_account_id IS NULL AND creator_identity_id IS NULL)
                OR (
                  creator_account_id IS NOT NULL
                  AND creator_identity_id IS NOT NULL
                )
              );
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        ALTER TABLE import_task
          ADD COLUMN IF NOT EXISTS creator_account_id BIGINT
            REFERENCES user_account(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS creator_identity_id BIGINT
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_import_task_creator_identity'
              AND conrelid='import_task'::regclass
          ) THEN
            ALTER TABLE import_task
              ADD CONSTRAINT fk_import_task_creator_identity
              FOREIGN KEY (creator_identity_id,creator_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_import_task_creator_principal_pair'
              AND conrelid='import_task'::regclass
          ) THEN
            ALTER TABLE import_task
              ADD CONSTRAINT ck_import_task_creator_principal_pair CHECK (
                (creator_account_id IS NULL AND creator_identity_id IS NULL)
                OR (
                  creator_account_id IS NOT NULL
                  AND creator_identity_id IS NOT NULL
                )
              );
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        ALTER TABLE vendor_test_operation
          ADD COLUMN IF NOT EXISTS actor_account_id BIGINT
            REFERENCES user_account(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS actor_identity_id BIGINT
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_vendor_test_operation_actor_identity'
              AND conrelid='vendor_test_operation'::regclass
          ) THEN
            ALTER TABLE vendor_test_operation
              ADD CONSTRAINT fk_vendor_test_operation_actor_identity
              FOREIGN KEY (actor_identity_id,actor_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_vendor_test_operation_actor_principal_pair'
              AND conrelid='vendor_test_operation'::regclass
          ) THEN
            ALTER TABLE vendor_test_operation
              ADD CONSTRAINT ck_vendor_test_operation_actor_principal_pair CHECK (
                (actor_account_id IS NULL AND actor_identity_id IS NULL)
                OR (
                  actor_account_id IS NOT NULL
                  AND actor_identity_id IS NOT NULL
                )
              );
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        ALTER TABLE export_task
        ADD COLUMN IF NOT EXISTS creator_identity_id BIGINT
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_export_creator_identity'
              AND conrelid='export_task'::regclass
          ) THEN
            ALTER TABLE export_task
              ADD CONSTRAINT fk_export_creator_identity
              FOREIGN KEY (creator_identity_id,creator_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
        END
        $migration$
        """
    )
    # 0024 时代只凭当时登录名回填的 account_id 无法证明历史所有权。
    # 0026 以 identity_id 作为新写入证据边界，升级前记录统一回到 unknown。
    op.execute(
        """
        UPDATE export_task
        SET creator_account_id=NULL
        WHERE creator_identity_id IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE approval
          ADD COLUMN IF NOT EXISTS applicant_account_id BIGINT
            REFERENCES user_account(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS applicant_identity_id BIGINT,
          ADD COLUMN IF NOT EXISTS approver_account_id BIGINT
            REFERENCES user_account(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS approver_identity_id BIGINT
        """
    )
    # 旧同名约束比较可变登录名；以同名的稳定账号 ID 约束原子替换。
    op.execute(
        """
        ALTER TABLE approval DROP CONSTRAINT IF EXISTS chk_no_self_approve
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_approval_applicant_identity'
              AND conrelid='approval'::regclass
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT fk_approval_applicant_identity
              FOREIGN KEY (applicant_identity_id,applicant_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_approval_approver_identity'
              AND conrelid='approval'::regclass
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT fk_approval_approver_identity
              FOREIGN KEY (approver_identity_id,approver_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_approval_applicant_principal_pair'
              AND conrelid='approval'::regclass
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT ck_approval_applicant_principal_pair CHECK (
                (applicant_account_id IS NULL AND applicant_identity_id IS NULL)
                OR (
                  applicant_account_id IS NOT NULL
                  AND applicant_identity_id IS NOT NULL
                )
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_approval_approver_principal_pair'
              AND conrelid='approval'::regclass
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT ck_approval_approver_principal_pair CHECK (
                (approver_account_id IS NULL AND approver_identity_id IS NULL)
                OR (
                  approver_account_id IS NOT NULL
                  AND approver_identity_id IS NOT NULL
                )
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='chk_no_self_approve'
              AND conrelid='approval'::regclass
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT chk_no_self_approve CHECK (
                approver_account_id IS NULL
                OR applicant_account_id IS NULL
                OR approver_account_id <> applicant_account_id
              );
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        ALTER TABLE audit_log
          ADD COLUMN IF NOT EXISTS actor_subject_kind VARCHAR(16)
            NOT NULL DEFAULT 'legacy_unknown'
            CHECK (
              actor_subject_kind IN ('human','api_app','system','legacy_unknown')
            ),
          ADD COLUMN IF NOT EXISTS actor_account_id BIGINT
            REFERENCES user_account(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS actor_identity_id BIGINT,
          ADD COLUMN IF NOT EXISTS actor_app_id BIGINT
            REFERENCES app(id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_audit_actor_identity'
              AND conrelid='audit_log'::regclass
          ) THEN
            ALTER TABLE audit_log
              ADD CONSTRAINT fk_audit_actor_identity
              FOREIGN KEY (actor_identity_id,actor_account_id)
              REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_audit_actor_subject'
              AND conrelid='audit_log'::regclass
          ) THEN
            ALTER TABLE audit_log
              ADD CONSTRAINT ck_audit_actor_subject CHECK (
                (
                  actor_subject_kind='human'
                  AND actor_account_id IS NOT NULL
                  AND actor_app_id IS NULL
                )
                OR (
                  actor_subject_kind='api_app'
                  AND actor_account_id IS NULL
                  AND actor_identity_id IS NULL
                  AND actor_app_id IS NOT NULL
                )
                OR (
                  actor_subject_kind IN ('system','legacy_unknown')
                  AND actor_account_id IS NULL
                  AND actor_identity_id IS NULL
                  AND actor_app_id IS NULL
                )
              );
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        UPDATE audit_log
        SET actor_subject_kind='human',
            actor_account_id=(after_val->>'actor_account_id')::bigint
        WHERE after_val ? 'actor_account_id'
          AND (after_val->>'actor_account_id') ~ '^[1-9][0-9]*$'
          AND EXISTS (
            SELECT 1 FROM user_account
            WHERE id=(audit_log.after_val->>'actor_account_id')::bigint
          )
        """
    )
    op.execute(
        """
        UPDATE audit_log
        SET actor_identity_id=(after_val->>'actor_identity_id')::bigint
        WHERE actor_subject_kind='human'
          AND after_val ? 'actor_identity_id'
          AND (after_val->>'actor_identity_id') ~ '^[1-9][0-9]*$'
          AND EXISTS (
            SELECT 1 FROM auth_identity
            WHERE id=(audit_log.after_val->>'actor_identity_id')::bigint
              AND account_id=audit_log.actor_account_id
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sms_batch_creator_account
        ON sms_batch(creator_account_id,created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_creator_account
        ON import_task(creator_account_id,created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_approval_applicant_account
        ON approval(applicant_account_id,created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_approval_approver_account
        ON approval(approver_account_id,decided_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vendor_test_operation_actor_account
        ON vendor_test_operation(actor_account_id,requested_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_actor_account
        ON audit_log(actor_account_id,created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_export_creator_account
        ON export_task(creator_account_id,created_at DESC)
        """
    )


def downgrade() -> None:
    """禁止恢复以可变登录名实施授权和职责分离的旧语义。"""

    raise RuntimeError("stable principal IDs downgrade is intentionally blocked")
