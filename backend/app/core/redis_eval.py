"""Redis Lua 脚本调用的最小异步接口。"""

from __future__ import annotations

from typing import Any, Protocol

RedisArg = str | int | float


class RedisEval(Protocol):
    """只暴露 EVAL；返回值形状由各 Lua 脚本决定，调用方按脚本约定解析。"""

    async def eval(self, script: str, numkeys: int, *keys_and_args: RedisArg) -> Any: ...
