from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.core.runtime_resources import close_runtime_resources


@pytest.fixture(autouse=True)
async def close_process_pools_after_integration_test() -> AsyncIterator[None]:
    """每个测试 loop 内关闭共享池，模拟对应服务进程的 shutdown。"""

    try:
        yield
    finally:
        await close_runtime_resources()
