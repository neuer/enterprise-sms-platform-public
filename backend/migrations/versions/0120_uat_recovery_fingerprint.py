"""受控联调恢复绑定请求指纹，并统一生命周期事实的最小权限。"""

from alembic import op

revision = "0120_uat_recovery_fingerprint"
down_revision = "0119_temporary_password_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE vendor_test_operation
          ADD COLUMN IF NOT EXISTS acceptance_request_hash VARCHAR(64),
          ADD COLUMN IF NOT EXISTS acceptance_key_version SMALLINT
    """)
    op.execute("""
        DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
          WHERE conrelid='vendor_test_operation'::regclass
            AND conname='ck_vendor_test_acceptance_fingerprint') THEN
          ALTER TABLE vendor_test_operation ADD CONSTRAINT ck_vendor_test_acceptance_fingerprint
          CHECK ((acceptance_request_hash IS NULL AND acceptance_key_version IS NULL)
            OR (acceptance_request_hash IS NOT NULL AND acceptance_key_version IS NOT NULL
                AND acceptance_request_hash ~ '^[0-9a-f]{64}$'
                AND acceptance_key_version BETWEEN 1 AND 32767));
        END IF; END $$;
    """)
    # 不从相同 biz_id 的任意旧批次回填未知请求；旧未绑定操作保留人工核验。
    op.execute(
        """REVOKE DELETE ON sms_uncertain_resolution, sms_uncertain_child,
           usage_chunk_allocation, usage_chunk_release, send_inflight_balance,
           send_inflight_reservation, send_inflight_reconcile_fact,
           send_admission_state, send_runtime_heartbeat FROM sms_send"""
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM vendor_test_operation
                     WHERE acceptance_request_hash IS NOT NULL) THEN
            RAISE EXCEPTION 'UAT recovery fingerprint downgrade unsafe';
          END IF;
        END $$;
    """)
    op.execute("""
        ALTER TABLE vendor_test_operation
          DROP CONSTRAINT IF EXISTS ck_vendor_test_acceptance_fingerprint,
          DROP COLUMN IF EXISTS acceptance_request_hash,
          DROP COLUMN IF EXISTS acceptance_key_version;
    """)
