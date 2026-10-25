"""列表分页与 bigint 标识的协议上界。

在进入 SQL 之前拒绝超出页码或 OFFSET 的参数，避免数据库把无效输入变成 500。
"""

from __future__ import annotations

PAGE_MAX = 1_000_000
SIGNED_BIGINT_MAX = 2**63 - 1


def ensure_page_offset(page: int, size: int) -> None:
    """页码与页大小必须使 OFFSET 落在有符号 bigint 内。"""

    if (
        type(page) is not int
        or type(size) is not int
        or page < 1
        or page > PAGE_MAX
        or size < 1
        or size > SIGNED_BIGINT_MAX
        or (page - 1) > SIGNED_BIGINT_MAX // size
    ):
        raise ValueError("page or size is out of bounds")


def ensure_bigint_id(value: int | None, *, name: str = "app_id") -> None:
    """BIGSERIAL/bigint 筛选只接受 1..2**63-1；None 表示不筛选。"""

    if value is None:
        return
    if type(value) is not int or value < 1 or value > SIGNED_BIGINT_MAX:
        raise ValueError(f"{name} is out of bounds")
