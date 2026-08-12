"""Harden LDAP authorization, blacklist HMAC rotation, and exact queries."""

from __future__ import annotations

from alembic import op

revision = "0066_security_findings_hardening"
down_revision = "0065_security_scan_round4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线（0001）已包含这些对象；真实 0065 旧库则尚未包含。
    # 因此后续迁移必须幂等，才能同时支持当前空库建库与存量升级。
    op.execute(
        "ALTER TABLE external_role_mapping "
        "ADD COLUMN IF NOT EXISTS dept VARCHAR(128)"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='external_role_mapping'::regclass
              AND conname='ck_external_role_mapping_dept'
          ) THEN
            ALTER TABLE external_role_mapping
              ADD CONSTRAINT ck_external_role_mapping_dept
              CHECK (dept IS NULL OR length(btrim(dept)) BETWEEN 1 AND 128);
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS blacklist_hmac_alias (
          blacklist_digest CHAR(64) NOT NULL,
          hmac_key_version SMALLINT NOT NULL
            CONSTRAINT ck_blacklist_hmac_alias_version
            CHECK (hmac_key_version BETWEEN 1 AND 32767),
          hmac_digest CHAR(64) NOT NULL
            CONSTRAINT ck_blacklist_hmac_alias_digest
            CHECK (hmac_digest ~ '^[0-9a-f]{64}$'),
          CONSTRAINT pk_blacklist_hmac_alias
            PRIMARY KEY (hmac_key_version,hmac_digest),
          CONSTRAINT uq_blacklist_hmac_alias_owner_version
            UNIQUE (blacklist_digest,hmac_key_version),
          CONSTRAINT fk_blacklist_hmac_alias_owner
            FOREIGN KEY (blacklist_digest) REFERENCES blacklist(phone_hmac)
            ON UPDATE CASCADE ON DELETE CASCADE
        )
        """
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
