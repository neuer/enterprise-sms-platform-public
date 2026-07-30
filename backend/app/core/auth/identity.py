"""认证身份登录名的统一规范化与本地账号校验。"""

from __future__ import annotations

import re

LOCAL_LOGIN_RE = re.compile(r"^[a-z0-9._-]{3,64}$")
LOCAL_LOGIN_RULE = "本地用户名必须为 3–64 位字母、数字、点、下划线或短横线"


class InvalidLoginName(ValueError):
    """登录名不符合平台规范。"""


def normalize_login_name(value: str) -> str:
    """去除首尾空格并按大小写不敏感规则生成唯一登录名。"""

    return value.strip().casefold()


def validate_local_login_name(value: str) -> str:
    """校验本地登录名并返回可直接持久化的规范化值。"""

    normalized = normalize_login_name(value)
    if LOCAL_LOGIN_RE.fullmatch(normalized) is None:
        raise InvalidLoginName(LOCAL_LOGIN_RULE)
    return normalized
