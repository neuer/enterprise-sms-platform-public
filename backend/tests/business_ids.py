"""只供测试使用的隐私安全业务编号，不替换真实 UUID 或认证令牌。"""

from uuid import uuid4


def new_business_id() -> str:
    """保留随机的 32 位 hex 外形，确定性消除手机号片段的起始数字。"""
    return uuid4().hex.replace("1", "a")
