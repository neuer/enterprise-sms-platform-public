"""独立回执超时扫描的有界索引与调度参数。"""

from __future__ import annotations

from alembic import op

revision = "0107_report_timeout_sweep"
down_revision = "0106_auth_transition_dead_letter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_msg_sent_timeout
            ON sms_message(batch_id, chunk_id, id, created_at)
            WHERE status = 'sent'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_submitted_timeout
            ON sms_chunk(submitted_at)
            WHERE submitted_at IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description) VALUES
          (
            'report_timeout_scan_seconds','60','int',
            '本地回执超时扫描间隔(秒,重启beat生效)'
          ),
          (
            'report_timeout_batch_limit','50','int',
            '每轮回执超时扫描最多领取批次数'
          ),
          (
            'report_timeout_message_limit','200','int',
            '单批回执超时扫描最多更新消息数'
          ),
          (
            'report_timeout_round_seconds','20','int',
            '回执超时扫描单轮时间预算(秒)'
          ),
          (
            'report_timeout_statement_ms','5000','int',
            '回执超时扫描语句超时(毫秒)'
          ),
          (
            'report_timeout_lock_ms','1000','int',
            '回执超时扫描锁等待超时(毫秒)'
          )
        ON CONFLICT(key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM sys_config WHERE key IN (
          'report_timeout_scan_seconds',
          'report_timeout_batch_limit',
          'report_timeout_message_limit',
          'report_timeout_round_seconds',
          'report_timeout_statement_ms',
          'report_timeout_lock_ms'
        )
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_chunk_submitted_timeout")
    op.execute("DROP INDEX IF EXISTS idx_msg_sent_timeout")
    return
