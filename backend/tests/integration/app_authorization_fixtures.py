"""既有数据库业务回归使用显式测试主体生成授权，不替换生产授权检查。"""

from typing import Any

from sqlalchemy import text

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.principal_context import current_audit_principal
from app.core.runtime_resources import database_engine
from app.services.admin_step_up import admin_intent
from app.services.app_contract import AppCreateRequest, AppUpdateRequest
from app.services.app_repository import SqlAppRepository


async def authorized_app_write(
    repository: SqlAppRepository,
    operation: str,
    app_id: int | None = None,
    **values: Any,
) -> Any:
    """只供隔离 PG 业务测试：授权准确绑定本次值和当前数据库版本。"""
    assert operation in {"create", "update", "disable", "rotate_key", "revoke_old_key"}
    principal = current_audit_principal()
    assert isinstance(principal, SecurityPrincipal) and principal.role == "admin"
    engine = database_engine(repository.settings.database_url_for("auth"))
    async with engine.connect() as c:
        version = await c.scalar(
            text("SELECT security_version FROM user_account WHERE id=:id"),
            {"id": principal.account_id},
        )
    parameters: dict[str, Any] = {}
    if operation in {"create", "update"}:
        model = AppCreateRequest if operation == "create" else AppUpdateRequest
        parameters["config"] = {key: values[key] for key in model.model_fields}
    if app_id is not None:
        current = await repository.get(app_id)
        assert current is not None
        parameters["expected_updated_at"] = current["updated_at"]
        values["expected_updated_at"] = current["updated_at"]
    grant = AdminAuthorization(
        principal.account_id,
        principal.identity_id,
        int(version),
        "local",
        admin_intent("app_" + operation, "new" if app_id is None else str(app_id), parameters),
    )
    if app_id is None:
        return await repository.create(authorization=grant, **values)
    return await getattr(repository, operation)(app_id, authorization=grant, **values)
