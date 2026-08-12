"""Harden LDAP authorization, blacklist HMAC rotation, and exact queries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_security_findings_hardening"
down_revision = "0065_security_scan_round4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "external_role_mapping",
        sa.Column("dept", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        "ck_external_role_mapping_dept",
        "external_role_mapping",
        "dept IS NULL OR length(btrim(dept)) BETWEEN 1 AND 128",
    )
    op.create_table(
        "blacklist_hmac_alias",
        sa.Column(
            "blacklist_digest",
            sa.CHAR(length=64),
            sa.ForeignKey(
                "blacklist.phone_hmac",
                name="fk_blacklist_hmac_alias_owner",
                onupdate="CASCADE",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("hmac_key_version", sa.SmallInteger(), nullable=False),
        sa.Column("hmac_digest", sa.CHAR(length=64), nullable=False),
        sa.CheckConstraint(
            "hmac_key_version BETWEEN 1 AND 32767",
            name="ck_blacklist_hmac_alias_version",
        ),
        sa.CheckConstraint(
            "hmac_digest ~ '^[0-9a-f]{64}$'",
            name="ck_blacklist_hmac_alias_digest",
        ),
        sa.PrimaryKeyConstraint(
            "hmac_key_version",
            "hmac_digest",
            name="pk_blacklist_hmac_alias",
        ),
        sa.UniqueConstraint(
            "blacklist_digest",
            "hmac_key_version",
            name="uq_blacklist_hmac_alias_owner_version",
        ),
    )
    op.execute(
        """
        INSERT INTO blacklist_hmac_alias(
          blacklist_digest,hmac_key_version,hmac_digest
        )
        SELECT phone_hmac,key_version,phone_hmac FROM blacklist
        """
    )
    op.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON blacklist_hmac_alias TO sms_accept"
    )
    op.execute("GRANT SELECT ON blacklist_hmac_alias TO sms_send")


def downgrade() -> None:
    op.drop_table("blacklist_hmac_alias")
    op.drop_constraint(
        "ck_external_role_mapping_dept",
        "external_role_mapping",
        type_="check",
    )
    op.drop_column("external_role_mapping", "dept")
