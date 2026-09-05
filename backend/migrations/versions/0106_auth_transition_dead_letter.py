"""认证 Transition 孤儿 Due 成员的运维死信，禁止猜测锁定/封禁事实。"""

from __future__ import annotations

from alembic import op

revision = "0106_auth_transition_dead_letter"
down_revision = "0105_admission_recovery_hold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_transition_dead_letter (
            id              BIGSERIAL PRIMARY KEY,
            transition_hmac VARCHAR(64) NOT NULL,
            reason          VARCHAR(32) NOT NULL,
            field_class     VARCHAR(32) NOT NULL,
            discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            build_version   VARCHAR(64) NOT NULL,
            CONSTRAINT ck_auth_transition_dl_reason CHECK (reason IN (
                'missing_hash','incomplete_envelope','id_mismatch'
            )),
            CONSTRAINT ck_auth_transition_dl_hmac CHECK (transition_hmac ~ '^[0-9a-f]{64}$'),
            CONSTRAINT uq_auth_transition_dl_hmac UNIQUE (transition_hmac)
        )
        """
    )
    op.execute("GRANT INSERT ON auth_transition_dead_letter TO sms_auth")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE auth_transition_dead_letter_id_seq TO sms_auth"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE USAGE, SELECT ON SEQUENCE auth_transition_dead_letter_id_seq FROM sms_auth"
    )
    op.execute("REVOKE INSERT ON auth_transition_dead_letter FROM sms_auth")
    op.execute("DROP TABLE IF EXISTS auth_transition_dead_letter")
    return
