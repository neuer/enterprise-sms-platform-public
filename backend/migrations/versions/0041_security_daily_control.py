"""Add the redacted security daily report fact and mailer control tables."""

from __future__ import annotations

from alembic import op

revision = "0041_security_daily_control"
down_revision = "0040_background_task_role_matrix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create API-owned report facts and the bounded independent-mailer queue."""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS security_daily_report (
            id                BIGSERIAL PRIMARY KEY,
            report_date       DATE NOT NULL UNIQUE,
            period_start      TIMESTAMPTZ NOT NULL,
            period_end        TIMESTAMPTZ NOT NULL,
            status             VARCHAR(16) NOT NULL
                              CHECK (status IN ('normal','attention','high')),
            generation_status  VARCHAR(16) NOT NULL DEFAULT 'unavailable'
                              CHECK (
                                generation_status IN ('pending','ready','failed','unavailable')
                              ),
            delivery_status    VARCHAR(16) NOT NULL DEFAULT 'not_sent'
                              CHECK (
                                delivery_status IN ('not_sent','pending','sending','sent','failed')
                              ),
            payload            JSONB,
            generated_at       TIMESTAMPTZ,
            delivered_at       TIMESTAMPTZ,
            recipient_count    SMALLINT NOT NULL DEFAULT 0
                              CHECK (recipient_count BETWEEN 0 AND 3),
            retry_count        SMALLINT NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            last_error         VARCHAR(256),
            last_error_at      TIMESTAMPTZ,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_security_daily_period CHECK (period_start < period_end),
            CONSTRAINT ck_security_daily_payload_ready CHECK (
              generation_status <> 'ready' OR payload IS NOT NULL
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_security_daily_report_status
            ON security_daily_report(status,report_date DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_security_daily_report_delivery
            ON security_daily_report(delivery_status,report_date DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS security_daily_delivery_request (
            request_id       UUID PRIMARY KEY,
            report_id        BIGINT NOT NULL
                             REFERENCES security_daily_report(id) ON DELETE RESTRICT,
            report_date      DATE NOT NULL,
            action           VARCHAR(8) NOT NULL CHECK (action IN ('send','retry')),
            state            VARCHAR(8) NOT NULL DEFAULT 'pending'
                             CHECK (state IN ('pending','sent','failed')),
            dedup_key        VARCHAR(192) NOT NULL UNIQUE,
            requested_by     VARCHAR(64) NOT NULL,
            requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at     TIMESTAMPTZ,
            error            VARCHAR(256),
            CONSTRAINT ck_security_daily_request_completion CHECK (
              (state='pending' AND completed_at IS NULL)
              OR (state IN ('sent','failed') AND completed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_security_daily_request_pending
            ON security_daily_delivery_request(state,requested_at)
            WHERE state='pending'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_security_daily_request_report
            ON security_daily_delivery_request(report_id,requested_at DESC)
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON
            security_daily_report, security_daily_delivery_request TO sms_accept
        """
    )
    op.execute("GRANT USAGE, SELECT ON SEQUENCE security_daily_report_id_seq TO sms_accept")
    op.execute("GRANT SELECT, INSERT, UPDATE ON security_daily_report TO sms_send")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE security_daily_report_id_seq TO sms_send")


def downgrade() -> None:
    """Remove only the tables introduced by this migration."""

    op.execute("REVOKE USAGE, SELECT ON SEQUENCE security_daily_report_id_seq FROM sms_accept")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON security_daily_report, "
        "security_daily_delivery_request FROM sms_accept"
    )
    op.execute("REVOKE USAGE, SELECT ON SEQUENCE security_daily_report_id_seq FROM sms_send")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON security_daily_report FROM sms_send")
    op.execute("DROP TABLE IF EXISTS security_daily_delivery_request")
    op.execute("DROP TABLE IF EXISTS security_daily_report")
