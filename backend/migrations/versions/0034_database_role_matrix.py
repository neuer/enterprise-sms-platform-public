"""按运行职责拆分数据库最小权限角色，并永久停用 sms_app。"""

from __future__ import annotations

from alembic import op

revision = "0034_database_role_matrix"
down_revision = "0033_correlation_audit_chain"
branch_labels = None
depends_on = None

RUNTIME_ROLES = (
    "sms_auth",
    "sms_accept",
    "sms_send",
    "sms_callback",
    "sms_export",
    "sms_scheduler",
    "sms_metrics",
)

ROLE_MATRIX_SQL = """
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM
  sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM
  sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM
  sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics;
GRANT USAGE ON SCHEMA public TO
  sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics;

GRANT SELECT ON
  user_account,auth_provider,auth_identity,local_credential,
  external_role_mapping,sys_config,app
TO sms_auth;
GRANT INSERT,UPDATE ON
  user_account,auth_provider,auth_identity,local_credential
TO sms_auth;
GRANT INSERT,UPDATE,DELETE ON external_role_mapping TO sms_auth;
GRANT INSERT ON audit_log TO sms_auth;
GRANT USAGE,SELECT ON SEQUENCE
  user_account_id_seq,auth_provider_id_seq,auth_identity_id_seq,
  external_role_mapping_id_seq,audit_log_id_seq
TO sms_auth;

GRANT SELECT ON
  app,dept_quota,sms_batch,idempotency_record,sms_chunk,sms_message,
  sms_reply,raw_vendor_log,report_event,report_event_projection,reply_event,
  unmatched_report,job_run,import_task,import_phone,approval,sms_template,
  sms_sign,blacklist,sensitive_word,callback_report_event,callback_task,
  worker_lease_event,balance_snapshot,alert_log,outbox_event,
  usage_reservation,usage_frequency_subject,usage_frequency_alias,
  usage_quota_entry,usage_frequency_entry,usage_projection,
  usage_projection_drift,stat_daily,sys_config,audit_log,export_task,
  vendor_test_daily_usage,vendor_test_send_attempt,vendor_test_recipient,
  vendor_test_recipient_hmac_alias,vendor_test_operation,alembic_version
TO sms_accept;
GRANT INSERT,UPDATE,DELETE ON
  app,dept_quota,sms_batch,idempotency_record,sms_message,
  import_task,import_phone,approval,sms_template,sms_sign,blacklist,
  sensitive_word,usage_reservation,usage_frequency_subject,
  usage_frequency_alias,usage_quota_entry,usage_frequency_entry,
  usage_projection,usage_projection_drift,sys_config,vendor_test_recipient
TO sms_accept;
GRANT INSERT ON callback_task,alert_log TO sms_accept;
GRANT INSERT,DELETE ON vendor_test_recipient_hmac_alias TO sms_accept;
GRANT INSERT,UPDATE ON
  outbox_event,vendor_test_daily_usage,vendor_test_send_attempt,
  vendor_test_operation
TO sms_accept;
GRANT SELECT,INSERT ON audit_log TO sms_accept;
GRANT USAGE,SELECT ON SEQUENCE
  app_id_seq,sms_batch_id_seq,idempotency_record_id_seq,
  sms_message_id_seq,import_task_id_seq,import_phone_id_seq,approval_id_seq,
  sms_template_id_seq,sms_sign_id_seq,sensitive_word_id_seq,audit_log_id_seq,
  vendor_test_send_attempt_id_seq,vendor_test_recipient_id_seq,
  callback_task_id_seq,alert_log_id_seq,
  usage_projection_version_seq
TO sms_accept;

GRANT SELECT ON
  app,dept_quota,sms_batch,idempotency_record,sms_chunk,sms_message,
  sms_reply,raw_vendor_log,report_event,report_event_projection,reply_event,
  unmatched_report,job_run,approval,sms_template,sms_sign,blacklist,
  sensitive_word,callback_report_event,callback_task,worker_lease_event,
  balance_snapshot,alert_log,outbox_event,usage_reservation,
  usage_frequency_subject,usage_frequency_alias,usage_quota_entry,
  usage_frequency_entry,usage_projection,usage_projection_drift,stat_daily,
  sys_config,vendor_test_daily_usage,vendor_test_send_attempt,
  vendor_test_recipient,vendor_test_recipient_hmac_alias,vendor_test_operation
TO sms_send;
GRANT INSERT,UPDATE,DELETE ON
  sms_batch,sms_chunk,sms_message,sms_reply,raw_vendor_log,
  unmatched_report,job_run,approval,balance_snapshot,alert_log,
  outbox_event,usage_reservation,usage_frequency_subject,
  usage_frequency_alias,usage_quota_entry,usage_frequency_entry,
  usage_projection,usage_projection_drift,stat_daily
TO sms_send;
GRANT INSERT ON
  report_event,reply_event,callback_report_event,worker_lease_event,audit_log
TO sms_send;
GRANT INSERT,UPDATE ON callback_task TO sms_send;
GRANT INSERT,UPDATE ON
  report_event_projection,vendor_test_daily_usage,
  vendor_test_send_attempt,vendor_test_operation
TO sms_send;
GRANT USAGE,SELECT ON SEQUENCE
  sms_batch_id_seq,sms_chunk_id_seq,sms_message_id_seq,sms_reply_id_seq,
  raw_vendor_log_id_seq,unmatched_report_id_seq,job_run_id_seq,
  approval_id_seq,balance_snapshot_id_seq,alert_log_id_seq,
  worker_lease_event_id_seq,vendor_test_send_attempt_id_seq,
  audit_log_id_seq,callback_task_id_seq,usage_projection_version_seq
TO sms_send;

GRANT SELECT ON
  app,sms_batch,sms_message,callback_report_event,callback_task,
  worker_lease_event,alert_log,outbox_event,sys_config,job_run
TO sms_callback;
GRANT INSERT,UPDATE,DELETE ON callback_report_event,callback_task TO sms_callback;
GRANT INSERT,UPDATE ON outbox_event,alert_log,job_run TO sms_callback;
GRANT INSERT ON worker_lease_event,audit_log TO sms_callback;
GRANT USAGE,SELECT ON SEQUENCE
  callback_task_id_seq,worker_lease_event_id_seq,alert_log_id_seq,audit_log_id_seq,
  job_run_id_seq
TO sms_callback;

GRANT SELECT ON
  app,sms_batch,sms_message,sms_reply,report_event_projection,unmatched_report,
  export_task,sys_config,outbox_event,worker_lease_event
TO sms_export;
GRANT INSERT,UPDATE,DELETE ON export_task TO sms_export;
GRANT INSERT,UPDATE ON outbox_event TO sms_export;
GRANT INSERT ON worker_lease_event,audit_log TO sms_export;
GRANT USAGE,SELECT ON SEQUENCE
  export_task_id_seq,worker_lease_event_id_seq,audit_log_id_seq
TO sms_export;

GRANT SELECT ON sys_config,job_run,outbox_event,alert_log TO sms_scheduler;
GRANT INSERT,UPDATE,DELETE ON job_run TO sms_scheduler;
GRANT INSERT,UPDATE ON outbox_event,alert_log TO sms_scheduler;
GRANT INSERT ON audit_log TO sms_scheduler;
GRANT USAGE,SELECT ON SEQUENCE
  job_run_id_seq,alert_log_id_seq,audit_log_id_seq
TO sms_scheduler;

GRANT SELECT ON
  sms_batch,sms_chunk,callback_task,worker_lease_event,job_run,
  usage_projection_drift,outbox_event,export_task
TO sms_metrics;

ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
  REVOKE ALL ON TABLES FROM
  sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics;
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM
  sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics;
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
  REVOKE ALL ON TABLES FROM sms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM sms_app;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM sms_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM sms_app;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM sms_app;
ALTER ROLE sms_app NOLOGIN;
"""


def upgrade() -> None:
    for statement in ROLE_MATRIX_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # 回滚保持 fail closed：只撤销新矩阵，不恢复历史广权限 sms_app。
    roles = ",".join(RUNTIME_ROLES)
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles}")
    op.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {roles}")
    op.execute("ALTER ROLE sms_app NOLOGIN")
