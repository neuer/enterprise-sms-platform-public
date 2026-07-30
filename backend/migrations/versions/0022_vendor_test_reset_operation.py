"""允许持久化受控的真实联调配置重置 operation。"""

from __future__ import annotations

from alembic import op

revision = "0022_vendor_test_reset_operation"
down_revision = "0021_approval_legacy_default"
branch_labels = None
depends_on = None

_CONSTRAINT = "vendor_test_operation_operation_type_check"
_EXISTING_OPERATION_TYPES = (
    "install_credentials",
    "rotate_credentials",
    "activate",
    "pause",
    "resume",
    "uat_send",
)


def _replace_constraint(operation_types: tuple[str, ...]) -> None:
    condition = "operation_type IN (" + ",".join(
        f"'{operation_type}'" for operation_type in operation_types
    ) + ")"
    op.drop_constraint(
        _CONSTRAINT,
        "vendor_test_operation",
        type_="check",
    )
    op.create_check_constraint(
        _CONSTRAINT,
        "vendor_test_operation",
        condition,
    )


def upgrade() -> None:
    """只扩展 operation 类型约束，不改动任何表中记录。"""

    _replace_constraint((*_EXISTING_OPERATION_TYPES, "reset_configuration"))


def downgrade() -> None:
    """恢复原 operation 类型集合。"""

    _replace_constraint(_EXISTING_OPERATION_TYPES)
