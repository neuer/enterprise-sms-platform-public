from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.core.sensitive_http import sensitive_http_request


@pytest.mark.asyncio
async def test_sensitive_http_scope_is_task_local_and_resets_on_failure(caplog: Any) -> None:
    entered = asyncio.Event()
    released = asyncio.Event()

    async def sensitive() -> None:
        try:
            with sensitive_http_request():
                entered.set()
                await released.wait()
                for name in ("httpx", "httpcore.http11", "httpcore.connection"):
                    logging.getLogger(name).warning("private synthetic marker")
                raise ValueError("synthetic failure")
        except ValueError:
            logging.getLogger("httpx").warning("scope restored")

    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(sensitive())
        await entered.wait()
        logging.getLogger("httpx").warning("ordinary concurrent request")
        released.set()
        await task
    assert "private synthetic marker" not in caplog.text
    assert "ordinary concurrent request" in caplog.text
    assert "scope restored" in caplog.text
