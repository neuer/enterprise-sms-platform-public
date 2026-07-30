"""增加账号级登录防爆破运行参数。"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_auth_runtime_config"
down_revision: str | None = "0012_callback_event_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO sys_config(key,value,value_type,description) VALUES
          ('login_fail_limit','5','int','同账号15分钟内失败次数上限'),
          ('login_lock_minutes','15','int','账号锁定时长(分钟)')
        ON CONFLICT(key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM sys_config WHERE key IN ('login_fail_limit','login_lock_minutes')"
    )
