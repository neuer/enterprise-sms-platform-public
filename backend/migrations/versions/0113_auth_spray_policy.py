"""喷洒阈值与准入快照版本升级；保留已有容量配置。"""

from __future__ import annotations

from alembic import op

revision = "0113_auth_spray_policy"
down_revision = "0112_auth_admission_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 既有单调 revision 触发器使旧进程拒绝新快照，避免同 revision 不同 digest。
    op.execute("""UPDATE sys_config SET value = (
      '{"spray_failures": 12,"spray_sources": 4,"spray_delay_ms": 250}'::jsonb
      || value::jsonb)::text WHERE key = 'auth_admission_policy'
    """)


def downgrade() -> None:
    op.execute("""UPDATE sys_config SET value =
      (value::jsonb - 'spray_failures' - 'spray_sources' - 'spray_delay_ms')::text
      WHERE key = 'auth_admission_policy'
    """)
