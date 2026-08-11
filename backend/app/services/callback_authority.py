"""应用回调权限变更与短期投递租约的互斥边界。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text


class CallbackAuthorityBusy(RuntimeError):
    """当前仍有受 fencing 的回调正在解密或投递。"""


async def lock_callback_authority(connection: Any, app_id: int) -> None:
    """按应用获取事务级互斥锁；不扩大 callback 角色的 app 写权限。"""

    await connection.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'callback-authority:' || CAST(:app_id AS text),0))"
        ),
        {"app_id": app_id},
    )


async def ensure_callback_authority_idle(connection: Any, app_id: int) -> None:
    """短事务锁定 app，并拒绝在有效投递租约期间提交撤销。"""

    await lock_callback_authority(connection, app_id)
    await connection.execute(
        text("SELECT id FROM app WHERE id=:app_id FOR UPDATE"),
        {"app_id": app_id},
    )
    await connection.execute(
        text(
            "DELETE FROM callback_authority_lease "
            "WHERE app_id=:app_id AND expires_at<=now()"
        ),
        {"app_id": app_id},
    )
    active = await connection.scalar(
        text("SELECT 1 FROM callback_authority_lease WHERE app_id=:app_id"),
        {"app_id": app_id},
    )
    if active is not None:
        raise CallbackAuthorityBusy("回调正在投递，请稍后重试配置变更")
