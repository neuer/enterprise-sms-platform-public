"""签名格式规范化与发送前审核状态解析。"""

from __future__ import annotations

from typing import Protocol


class SignNotApproved(ValueError):
    """签名不存在或尚未审核通过。"""


def format_sign_name(name: str) -> str:
    """把平台裸签名稳定转换为厂商和计费共同使用的中文方括号格式。"""

    value = name.strip()
    if value.startswith("【") and value.endswith("】"):
        value = value[1:-1].strip()
    if not value or len(value) > 20 or "【" in value or "】" in value:
        raise ValueError("签名名称必须为 1 到 20 字且不得包含方括号")
    return f"【{value}】"


class ApprovedSignRepository(Protocol):
    async def is_approved(self, plain_name: str) -> bool: ...


class SignResolver:
    def __init__(self, repository: ApprovedSignRepository) -> None:
        self.repository = repository

    async def resolve(self, name: str) -> str:
        formatted = format_sign_name(name)
        plain = formatted[1:-1]
        if not await self.repository.is_approved(plain):
            raise SignNotApproved("签名不存在或未审核通过")
        return formatted
