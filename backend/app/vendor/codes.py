"""智慧信息厂商错误码与平台处置策略的完整映射。"""

from __future__ import annotations

from dataclasses import dataclass

BACKOFF_DELAYS_S = (1, 2, 4, 8, 16)


@dataclass(frozen=True, slots=True)
class VendorErrorPolicy:
    """业务层处理厂商错误所需的无歧义策略。"""

    description: str
    retry_delays_s: tuple[int, ...] = ()
    delay_s: int | None = None
    pause_queues: bool = False
    balance_blocked: bool = False
    shrink_batch_once: bool = False
    alert_level: str | None = None
    sync_resource: str | None = None
    not_applicable: bool = False
    return_to_caller: bool = False


def _policy(description: str, **overrides: object) -> VendorErrorPolicy:
    return VendorErrorPolicy(description=description, **overrides)  # type: ignore[arg-type]


ERROR_POLICIES: dict[int, VendorErrorPolicy] = {
    9: _policy("失败"),
    429: _policy("请求过多", retry_delays_s=BACKOFF_DELAYS_S),
    999: _policy(
        "余额不足",
        balance_blocked=True,
        pause_queues=True,
        alert_level="crit",
    ),
    1000: _policy("账号或密码错误", pause_queues=True, alert_level="crit"),
    1001: _policy("手机号码错误"),
    1002: _policy("内容格式错误"),
    1003: _policy("模板 id 错误", sync_resource="template"),
    1004: _policy("定时时间格式错误", not_applicable=True),
    1005: _policy("自定义 id 超过 36 位", alert_level="crit"),
    1006: _policy("号码数达到上限", shrink_batch_once=True),
    1007: _policy("定时时间小于 10 分钟", not_applicable=True),
    1008: _policy("未支持的套餐", alert_level="crit"),
    1009: _policy("账户未启用", pause_queues=True, alert_level="crit"),
    1010: _policy("IP 校验未通过", alert_level="crit"),
    1011: _policy("未在服务时间范围", delay_s=1800),
    1012: _policy("内容字数达到上限"),
    5000: _policy("缺省参数未配置", pause_queues=True, alert_level="crit"),
    5001: _policy("其他错误"),
    5002: _policy("调用间隔过快", retry_delays_s=BACKOFF_DELAYS_S),
    5003: _policy("每秒调用频次过快", retry_delays_s=BACKOFF_DELAYS_S),
    10000: _policy("扩展号错误", alert_level="crit"),
    10001: _policy("短信签名错误", sync_resource="sign"),
    10002: _policy("短信模板不匹配", sync_resource="template"),
    10003: _policy("短信密钥账号不存在", pause_queues=True, alert_level="crit"),
    10004: _policy("未激活短信业务", pause_queues=True, alert_level="crit"),
    10005: _policy("内容存在全局关键字"),
    10006: _policy("Excel 短信格式错误", not_applicable=True),
    10007: _policy("短信内容中存在签名"),
    10008: _policy("未开通短信模板", alert_level="crit"),
    10009: _policy("连接方式错误", alert_level="crit"),
    10010: _policy("当前操作已被锁定", delay_s=300),
    10011: _policy("模板数量达到上限", return_to_caller=True),
    10012: _policy("存在相同模板", return_to_caller=True),
    10013: _policy("存在相同签名", return_to_caller=True),
    10014: _policy("签名数量达到上限", return_to_caller=True),
}


UNKNOWN_ERROR_POLICY = VendorErrorPolicy("未知厂商错误")


def policy_for(code: int) -> VendorErrorPolicy:
    """返回已知错误策略；未知错误安全地按失败且不重试处理。"""

    return ERROR_POLICIES.get(code, UNKNOWN_ERROR_POLICY)
