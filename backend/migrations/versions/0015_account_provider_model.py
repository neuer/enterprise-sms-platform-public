"""以主体、认证源、身份和凭据替换用户名中心账号表。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_account_provider_model"
down_revision: str | None = "0014_security_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空数据系统直接重建账号模型，并兼容最新版 schema 基线重复执行。"""

    op.execute("DROP TABLE IF EXISTS role_mapping")
    op.execute("DROP TABLE IF EXISTS sys_user")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_account (
            id BIGSERIAL PRIMARY KEY,
            display_name VARCHAR(128) NOT NULL DEFAULT '',
            dept VARCHAR(128) NOT NULL DEFAULT '',
            role VARCHAR(16) NOT NULL DEFAULT 'viewer'
              CHECK (role IN ('admin','approver','operator','viewer')),
            role_override BOOLEAN NOT NULL DEFAULT TRUE,
            status SMALLINT NOT NULL DEFAULT 1 CHECK (status IN (0,1)),
            last_login_at TIMESTAMPTZ,
            auth_version BIGINT NOT NULL DEFAULT 1 CHECK (auth_version > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_provider (
            id BIGSERIAL PRIMARY KEY,
            code VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(128) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            draft_config JSONB NOT NULL DEFAULT '{}'::jsonb
              CHECK (jsonb_typeof(draft_config) = 'object'),
            active_config JSONB
              CHECK (active_config IS NULL OR jsonb_typeof(active_config) = 'object'),
            draft_version BIGINT NOT NULL DEFAULT 1 CHECK (draft_version > 0),
            tested_version BIGINT CHECK (tested_version IS NULL OR tested_version > 0),
            active_version BIGINT CHECK (active_version IS NULL OR active_version > 0),
            last_tested_at TIMESTAMPTZ,
            last_test_status VARCHAR(16)
              CHECK (last_test_status IS NULL OR last_test_status IN ('success','failed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO auth_provider (code, name, kind, enabled) VALUES
          ('local', '本地账号', 'local', TRUE),
          ('ad', 'AD 账号', 'ldap', FALSE)
        ON CONFLICT (code) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_identity (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL REFERENCES user_account(id) ON DELETE RESTRICT,
            provider_id BIGINT NOT NULL REFERENCES auth_provider(id) ON DELETE RESTRICT,
            login_name VARCHAR(64) NOT NULL,
            normalized_login_name VARCHAR(64) NOT NULL,
            external_subject VARCHAR(256) NOT NULL,
            status SMALLINT NOT NULL DEFAULT 1 CHECK (status IN (0,1)),
            source_groups TEXT[] NOT NULL DEFAULT '{}',
            last_synced_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (normalized_login_name),
            UNIQUE (provider_id, external_subject),
            CHECK (normalized_login_name = lower(btrim(login_name)))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_identity_account "
        "ON auth_identity(account_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_identity_provider "
        "ON auth_identity(provider_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS local_credential (
            identity_id BIGINT PRIMARY KEY
              REFERENCES auth_identity(id) ON DELETE RESTRICT,
            password_hash TEXT NOT NULL,
            must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
            password_changed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS external_role_mapping (
            id BIGSERIAL PRIMARY KEY,
            provider_id BIGINT NOT NULL
              REFERENCES auth_provider(id) ON DELETE CASCADE,
            external_group VARCHAR(256) NOT NULL,
            role VARCHAR(16) NOT NULL
              CHECK (role IN ('admin','approver','operator','viewer')),
            UNIQUE (provider_id, external_group)
        )
        """
    )


def downgrade() -> None:
    """破坏性恢复空旧表；本迁移明确不保留任何账号数据。"""

    op.execute("DROP TABLE IF EXISTS external_role_mapping")
    op.execute("DROP TABLE IF EXISTS local_credential")
    op.execute("DROP TABLE IF EXISTS auth_identity")
    op.execute("DROP TABLE IF EXISTS auth_provider")
    op.execute("DROP TABLE IF EXISTS user_account")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sys_user (
            id BIGSERIAL PRIMARY KEY,
            username VARCHAR(64) NOT NULL UNIQUE,
            display_name VARCHAR(128) NOT NULL DEFAULT '',
            dept VARCHAR(128) NOT NULL DEFAULT '',
            role VARCHAR(16) NOT NULL DEFAULT 'viewer'
              CHECK (role IN ('admin','approver','operator','viewer')),
            role_override BOOLEAN NOT NULL DEFAULT FALSE,
            source_groups TEXT[] NOT NULL DEFAULT '{}',
            last_synced_at TIMESTAMPTZ,
            status SMALLINT NOT NULL DEFAULT 1,
            last_login_at TIMESTAMPTZ,
            fail_count SMALLINT NOT NULL DEFAULT 0,
            locked_until TIMESTAMPTZ,
            auth_version BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS role_mapping (
            id BIGSERIAL PRIMARY KEY,
            ad_group VARCHAR(256) NOT NULL UNIQUE,
            role VARCHAR(16) NOT NULL
              CHECK (role IN ('admin','approver','operator','viewer'))
        )
        """
    )
