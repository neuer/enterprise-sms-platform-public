"""队列暂停声明：Redis 存 fencing claim，对外只暴露原因码。"""

from __future__ import annotations


def parse_queue_pause_claim(value: object) -> str | None:
    """把 Redis 暂停声明还原为对外原因码。

    厂商熔断写入 `{code}:{generation}`；历史裸码与 vendor-test 文本声明原样返回。
    """

    if value is None:
        return None
    claim = str(value)
    if not claim:
        return None
    code, separator, generation = claim.partition(":")
    if separator and code.isdigit() and generation.isdigit():
        return code
    return claim
