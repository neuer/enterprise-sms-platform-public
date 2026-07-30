"""真实联调跨进程生命周期互斥协议。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

VENDOR_TEST_LIFECYCLE_LOCK = "vendor-test-lifecycle"


async def lock_vendor_test_lifecycle(connection: Any) -> None:
    """持有至当前 PostgreSQL 事务结束，串行化 start 与破坏性收尾。"""

    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
        {"lock_name": VENDOR_TEST_LIFECYCLE_LOCK},
    )
