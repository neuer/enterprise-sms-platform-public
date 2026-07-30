"""收窄 metrics 为列级只读授权，隔离敏感业务字段。"""

from __future__ import annotations

from alembic import op

revision = "0037_metrics_access_boundary"
down_revision = "0036_callback_crypto_context"
branch_labels = None
depends_on = None

METRICS_COLUMN_GRANTS_SQL = """
REVOKE ALL PRIVILEGES ON
  sms_batch,sms_chunk,callback_task,worker_lease_event,job_run,
  usage_projection_drift,outbox_event,export_task
FROM sms_metrics;
GRANT SELECT (id,category,created_at,removed_freq) ON sms_batch TO sms_metrics;
GRANT SELECT (batch_id,phone_count,submitted_at,vendor_code,status)
  ON sms_chunk TO sms_metrics;
GRANT SELECT (status,lease_id,lease_expires_at)
  ON callback_task TO sms_metrics;
GRANT SELECT (task_kind,event_type)
  ON worker_lease_event TO sms_metrics;
GRANT SELECT (job_name,status,finished_at)
  ON job_run TO sms_metrics;
GRANT SELECT (kind,mismatched_dimensions,absolute_delta)
  ON usage_projection_drift TO sms_metrics;
GRANT SELECT (lease_id,lease_expires_at)
  ON export_task TO sms_metrics;
"""


def upgrade() -> None:
    for statement in METRICS_COLUMN_GRANTS_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # 回滚恢复 0034 的只读集合，但不授予任何写权限。
    op.execute(
        """
        GRANT SELECT ON
          sms_batch,sms_chunk,callback_task,worker_lease_event,job_run,
          usage_projection_drift,outbox_event,export_task
        TO sms_metrics
        """
    )
