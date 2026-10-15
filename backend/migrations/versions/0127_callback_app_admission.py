"""按应用查询回调准入事实的部分索引。"""

from alembic import op

revision = "0127_callback_app_admission"
down_revision = "0126_admission_recovery_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_cb_app_admission ON callback_task(app_id,status)
        WHERE status IN ('pending','retrying','dead')
    """)


def downgrade() -> None:
    # 允许旧应用回退，保留索引及全部回调事实。
    pass
