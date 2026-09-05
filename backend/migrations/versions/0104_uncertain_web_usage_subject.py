"""Web unknown 重发使用受控 system_effect 主体，禁止负数 app_id。"""

from __future__ import annotations

from alembic import op

revision = "0104_uncertain_web_usage_subject"
down_revision = "0103_inflight_split_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 已装入占用/守恒延迟触发器时，同事务 ALTER 可能留下 pending
    # trigger events；先立即校验再改表。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        ALTER TABLE app
          ADD COLUMN IF NOT EXISTS usage_subject_kind VARCHAR(16)
            NOT NULL DEFAULT 'api_app'
        """
    )
    op.execute("ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_usage_subject_kind")
    op.execute(
        """
        ALTER TABLE app
          ADD CONSTRAINT ck_app_usage_subject_kind
          CHECK (usage_subject_kind IN ('api_app','system_effect'))
        """
    )
    op.execute("ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_system_effect_quota")
    op.execute(
        """
        ALTER TABLE app
          ADD CONSTRAINT ck_app_system_effect_quota
          CHECK (usage_subject_kind <> 'system_effect' OR daily_quota > 0)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_usage_subject_kind
          ON app(usage_subject_kind)
          WHERE usage_subject_kind = 'system_effect'
        """
    )
    op.execute(
        """
        INSERT INTO app (
          id, name, dept, api_key_hash, api_key_prefix, allowed_categories,
          daily_quota, rate_limit_per_min, max_in_flight_chunks,
          blacklist_check, allowed_ips, usage_subject_kind, created_by
        ) VALUES (
          1000000001,
          'system-uncertain-resend',
          'system',
          '0000000000000000000000000000000000000000000000000000000000000000',
          'sysresnd',
          'verify,notice,market',
          10000,
          60,
          200,
          TRUE,
          '{}',
          'system_effect',
          'system'
        )
        ON CONFLICT (name) DO NOTHING
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_resolution
          ADD COLUMN IF NOT EXISTS source_dept VARCHAR(128)
        """
    )
    op.execute(
        """
        ALTER TABLE sms_uncertain_child
          ADD COLUMN IF NOT EXISTS recovered BOOLEAN
            NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          ADD COLUMN IF NOT EXISTS subject_kind VARCHAR(16)
            NOT NULL DEFAULT 'api_app'
        """
    )
    op.execute(
        "ALTER TABLE usage_reservation "
        "DROP CONSTRAINT IF EXISTS ck_usage_reservation_subject_kind"
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          ADD CONSTRAINT ck_usage_reservation_subject_kind
          CHECK (subject_kind IN ('api_app','system_effect'))
        """
    )
    op.execute(
        "ALTER TABLE usage_reservation "
        "DROP CONSTRAINT IF EXISTS ck_usage_reservation_system_effect_app"
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          ADD CONSTRAINT ck_usage_reservation_system_effect_app
          CHECK (subject_kind <> 'system_effect' OR app_id >= 1)
        """
    )
    op.execute(
        """
        UPDATE sms_uncertain_resolution r
        SET source_dept = b.dept
        FROM sms_batch b
        WHERE r.batch_id = b.id
          AND r.source_dept IS NULL
          AND b.dept IS NOT NULL
          AND btrim(b.dept) <> ''
        """
    )
    op.execute(
        """
        UPDATE sms_uncertain_resolution
        SET state = 'manual_intervention_required',
            effect_error = 'source_context_invalid'
        WHERE state IN ('effect_pending','retryable_effect_error','applying')
          AND COALESCE(source_channel, '') = 'web'
          AND (source_dept IS NULL OR btrim(source_dept) = '')
        """
    )
    op.execute(
        """
        GRANT SELECT (state, action, source_channel, confirmed_at, approved_at,
                      effect_error)
          ON sms_uncertain_resolution TO sms_metrics
        """
    )
    op.execute(
        """
        GRANT SELECT (generation, recovered, created_at)
          ON sms_uncertain_child TO sms_metrics
        """
    )


def downgrade() -> None:
    # 回滚保留 system app、source_dept、subject_kind 与 child 唯一事实（#633）。
    return
