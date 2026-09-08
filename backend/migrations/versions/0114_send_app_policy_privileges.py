"""内部发送只读取应用业务策略，不读取 API Key 认证材料。"""
from __future__ import annotations

from alembic import op

revision = "0114_send_app_policy_privileges"
down_revision = "0113_auth_spray_policy"
branch_labels = None
depends_on = None




def upgrade() -> None:
    op.execute("REVOKE SELECT ON app FROM sms_send")
    op.execute("""GRANT SELECT (id,name,dept,allowed_categories,default_sign,daily_quota,
rate_limit_per_min,recipient_limit_per_min,segment_limit_per_min,max_in_flight_chunks,
allow_market_api_bulk,blacklist_check,freq_override,allowed_ips,ip_allowlist_exempt_until,
unlimited_quota_exempt_until,admission_exempt_note,usage_subject_kind,callback_url,
callback_secret_enc,callback_report_enabled,status,created_by,created_at,updated_at)
ON app TO sms_send""")


def downgrade() -> None:
    op.execute("""REVOKE SELECT (id,name,dept,allowed_categories,default_sign,daily_quota,
rate_limit_per_min,recipient_limit_per_min,segment_limit_per_min,max_in_flight_chunks,
allow_market_api_bulk,blacklist_check,freq_override,allowed_ips,ip_allowlist_exempt_until,
unlimited_quota_exempt_until,admission_exempt_note,usage_subject_kind,callback_url,
callback_secret_enc,callback_report_enabled,status,created_by,created_at,updated_at)
ON app FROM sms_send""")
    op.execute("GRANT SELECT ON app TO sms_send")
