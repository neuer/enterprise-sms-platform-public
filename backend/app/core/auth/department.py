"""账号部门输入与授权快照的共享边界。"""


class InvalidAccountDepartment(ValueError):
    """角色与部门组合不能形成有效账号状态。"""


def normalize_account_department(role: str, dept: str) -> str:
    """规范化管理输入；管理员可无部门，其余角色必须绑定非空部门。"""
    normalized = dept.strip()
    if (
        role not in {"admin", "operator", "viewer", "approver"}
        or len(normalized) > 128
        or (role != "admin" and not normalized)
    ):
        raise InvalidAccountDepartment("当前角色必须指定有效部门（最多 128 字符）")
    return normalized


def valid_account_department(role: str, dept: str) -> bool:
    """历史授权快照必须已经规范化，不能在读权限时静默改变其部门。"""
    try:
        return normalize_account_department(role, dept) == dept
    except InvalidAccountDepartment:
        return False


def require_account_department(role: str, dept: str) -> None:
    """在范围构造前拒绝无效主体，保留受控 403 错误语义。"""
    from app.core.errors import ApiError

    if not valid_account_department(role, dept):
        raise ApiError(403, "FORBIDDEN", "账号部门无效，请联系管理员", None)
