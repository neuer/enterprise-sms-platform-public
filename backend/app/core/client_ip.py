"""可信真实客户端 IP 的唯一来源；所有安全判定必须复用本 helper。"""

from __future__ import annotations

import ipaddress

from fastapi import Request


def trusted_client_ip(request: Request) -> str:
    """返回经 Uvicorn 受信代理链规范化后的真实终端地址。

    `request.client.host` 只信任 Uvicorn ``--proxy-headers`` +
    ``--forwarded-allow-ips`` 生成的地址；解析失败时返回 ``0.0.0.0``
    作为不可信哨兵，绝不回退到客户端可控字符串。
    """

    if request.client is None:
        return "0.0.0.0"
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return "0.0.0.0"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)
