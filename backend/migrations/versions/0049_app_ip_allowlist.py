"""应用来源 IP/CIDR 白名单列。"""

from __future__ import annotations

from alembic import op

revision = "0049_app_ip_allowlist"
down_revision = "0048_security_daily_audit_accept"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 app 增加规范化来源 CIDR 白名单；空数组表示不限制。"""

    # schema.sql 基线（0001）已包含该列，这里必须幂等，与既有 add-column 迁移惯例一致。
    op.execute(
        "ALTER TABLE app "
        "ADD COLUMN IF NOT EXISTS allowed_ips TEXT[] NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    """移除来源白名单列。"""

    op.drop_column("app", "allowed_ips")
