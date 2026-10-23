"""人工 RAW 重放补齐 sms_accept 现有语句所需的最小列权限。

不授予 raw 密文、指纹、source、system_replay_audit_state，
不授予 sms_chunk.status/id 或事件事实的 UPDATE/DELETE。
匹配报告子查询额外只读 sms_vendor_attempt 的 vendor_id、chunk_id、outcome。
"""

from alembic import op

revision = "0129_sms_accept_raw_replay"
down_revision = "0128_runtime_event_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT UPDATE (custom_ids, item_count) ON raw_vendor_log TO sms_accept"
    )
    op.execute("GRANT UPDATE (late_evidence_at) ON sms_chunk TO sms_accept")
    op.execute(
        """
        GRANT INSERT (
            event_key, raw_id, vendor_task_id, custom_id,
            phone_enc, phone_hmac, phone_mask, key_version,
            report_status, message_status, report_desc, report_time
        ) ON report_event TO sms_accept
        """
    )
    op.execute(
        """
        GRANT INSERT (
            event_key, batch_id, message_id, message_created_at, projection_changed
        ) ON report_event_projection TO sms_accept
        """
    )
    op.execute(
        "GRANT UPDATE (projection_changed) ON report_event_projection TO sms_accept"
    )
    op.execute(
        """
        GRANT INSERT (
            event_key, event_key_version, raw_id, vendor_task_id, custom_id,
            phone_enc, phone_hmac, phone_mask, key_version,
            ext_code, content, content_enc, is_optout, reply_time
        ) ON reply_event TO sms_accept
        """
    )
    op.execute(
        """
        GRANT INSERT (
            event_key, vendor_task_id, batch_id,
            phone_enc, phone_hmac, phone_mask, key_version,
            ext_code, content, reply_time
        ) ON sms_reply TO sms_accept
        """
    )
    op.execute(
        """
        GRANT INSERT (
            event_key, vendor_task_id, custom_id,
            phone_enc, phone_hmac, phone_mask,
            key_version, report_status, report_desc, report_time
        ) ON unmatched_report TO sms_accept
        """
    )
    op.execute(
        """
        GRANT INSERT (
            event_key, batch_id, message_id, message_created_at,
            message_status, report_desc, report_time
        ) ON callback_report_event TO sms_accept
        """
    )
    op.execute(
        """
        GRANT UPDATE (message_ids, message_times, event_keys)
        ON callback_task TO sms_accept
        """
    )
    op.execute(
        """
        GRANT USAGE, SELECT ON SEQUENCE
            sms_reply_id_seq, unmatched_report_id_seq
        TO sms_accept
        """
    )
    op.execute(
        "GRANT SELECT (vendor_id, chunk_id, outcome) ON sms_vendor_attempt TO sms_accept"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (vendor_id, chunk_id, outcome) ON sms_vendor_attempt FROM sms_accept"
    )
    op.execute(
        """
        REVOKE USAGE, SELECT ON SEQUENCE
            sms_reply_id_seq, unmatched_report_id_seq
        FROM sms_accept
        """
    )
    op.execute(
        """
        REVOKE UPDATE (message_ids, message_times, event_keys)
        ON callback_task FROM sms_accept
        """
    )
    op.execute(
        """
        REVOKE INSERT (
            event_key, batch_id, message_id, message_created_at,
            message_status, report_desc, report_time
        ) ON callback_report_event FROM sms_accept
        """
    )
    op.execute(
        """
        REVOKE INSERT (
            event_key, vendor_task_id, custom_id,
            phone_enc, phone_hmac, phone_mask,
            key_version, report_status, report_desc, report_time
        ) ON unmatched_report FROM sms_accept
        """
    )
    op.execute(
        """
        REVOKE INSERT (
            event_key, vendor_task_id, batch_id,
            phone_enc, phone_hmac, phone_mask, key_version,
            ext_code, content, reply_time
        ) ON sms_reply FROM sms_accept
        """
    )
    op.execute(
        """
        REVOKE INSERT (
            event_key, event_key_version, raw_id, vendor_task_id, custom_id,
            phone_enc, phone_hmac, phone_mask, key_version,
            ext_code, content, content_enc, is_optout, reply_time
        ) ON reply_event FROM sms_accept
        """
    )
    op.execute(
        "REVOKE UPDATE (projection_changed) ON report_event_projection FROM sms_accept"
    )
    op.execute(
        """
        REVOKE INSERT (
            event_key, batch_id, message_id, message_created_at, projection_changed
        ) ON report_event_projection FROM sms_accept
        """
    )
    op.execute(
        """
        REVOKE INSERT (
            event_key, raw_id, vendor_task_id, custom_id,
            phone_enc, phone_hmac, phone_mask, key_version,
            report_status, message_status, report_desc, report_time
        ) ON report_event FROM sms_accept
        """
    )
    op.execute("REVOKE UPDATE (late_evidence_at) ON sms_chunk FROM sms_accept")
    op.execute(
        "REVOKE UPDATE (custom_ids, item_count) ON raw_vendor_log FROM sms_accept"
    )
