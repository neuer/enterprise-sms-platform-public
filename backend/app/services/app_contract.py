"""应用写入与二次认证共用的非凭据参数契约。"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, with_config
from typing_extensions import TypedDict

Category = Literal["verify", "notice", "market"]
IpCidr = Annotated[str, Field(max_length=64)]


@with_config(ConfigDict(extra="forbid"))
class FrequencyOverride(TypedDict, total=False):
    """应用级号码频控覆盖；拒绝未定义键及隐式类型转换。"""

    verify_per_minute: Annotated[int, Field(strict=True, ge=1, le=100)]
    verify_per_day: Annotated[int, Field(strict=True, ge=1, le=10_000)]
    market_per_day: Annotated[int, Field(strict=True, ge=1, le=1_000)]


def _frequency_values(value: FrequencyOverride | None) -> dict[str, int] | None:
    if value is None:
        return None
    result: dict[str, int] = {}
    if "verify_per_minute" in value:
        result["verify_per_minute"] = value["verify_per_minute"]
    if "verify_per_day" in value:
        result["verify_per_day"] = value["verify_per_day"]
    if "market_per_day" in value:
        result["market_per_day"] = value["market_per_day"]
    return result or None


class AppCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dept: str
    allowed_categories: list[Category] = ["notice"]
    default_sign: str | None = Field(default=None, max_length=32)
    daily_quota: int = Field(default=0, ge=0, le=100_000_000)
    rate_limit_per_min: int = Field(default=60, ge=1, le=60_000)
    recipient_limit_per_min: int = Field(default=10_000, ge=1, le=100_000_000)
    segment_limit_per_min: int = Field(default=10_000, ge=1, le=100_000_000)
    max_in_flight_chunks: int = Field(default=200, ge=1, le=100_000)
    allow_market_api_bulk: bool = False
    blacklist_check: bool = True
    freq_override: FrequencyOverride | None = None
    allowed_ips: list[IpCidr] = Field(
        default_factory=list,
        max_length=50,
        description="入站来源 IP/CIDR 白名单；生产空数组须填写未过期豁免",
    )
    ip_allowlist_exempt_until: datetime | None = None
    unlimited_quota_exempt_until: datetime | None = None
    admission_exempt_note: str | None = Field(default=None, max_length=200)
    callback_url: str | None = Field(default=None, max_length=256)
    callback_report_enabled: bool = False


class AppUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dept: str
    allowed_categories: list[Category]
    default_sign: str | None = Field(default=None, max_length=32)
    daily_quota: int = Field(ge=0, le=100_000_000)
    rate_limit_per_min: int = Field(ge=1, le=60_000)
    recipient_limit_per_min: int = Field(default=10_000, ge=1, le=100_000_000)
    segment_limit_per_min: int = Field(default=10_000, ge=1, le=100_000_000)
    max_in_flight_chunks: int = Field(default=200, ge=1, le=100_000)
    allow_market_api_bulk: bool = False
    blacklist_check: bool
    freq_override: FrequencyOverride | None = None
    allowed_ips: list[IpCidr] = Field(
        default_factory=list,
        max_length=50,
        description="入站来源 IP/CIDR 白名单；生产空数组须填写未过期豁免",
    )
    ip_allowlist_exempt_until: datetime | None = None
    unlimited_quota_exempt_until: datetime | None = None
    admission_exempt_note: str | None = Field(default=None, max_length=200)
    callback_url: str | None = Field(default=None, max_length=256)
    callback_report_enabled: bool = False
    status: Literal[0, 1] = 1


def canonical_app_parameters(values: dict[str, Any], *, create: bool) -> dict[str, Any]:
    """只接纳配置白名单；签发和实际 SQL 值统一为同一确定性表示。"""
    model = AppCreateRequest if create else AppUpdateRequest
    if set(values) != set(model.model_fields):
        raise ValueError("应用授权必须包含完整配置且不得包含凭据")
    prepared = dict(values)
    if isinstance(prepared.get("allowed_categories"), str):
        prepared["allowed_categories"] = prepared["allowed_categories"].split(",")
    validated = model.model_validate(prepared)
    result = validated.model_dump()
    result["allowed_categories"] = sorted(set(result["allowed_categories"]))
    result["allowed_ips"] = sorted(
        {str(ipaddress.ip_network(item.strip(), strict=False)) for item in result["allowed_ips"]}
    )
    result["freq_override"] = result["freq_override"] or None
    note = result["admission_exempt_note"]
    result["admission_exempt_note"] = (note.strip() or None) if note else None
    result["callback_url"] = result["callback_url"] or None
    for key in ("ip_allowlist_exempt_until", "unlimited_quota_exempt_until"):
        if result[key] is not None:
            result[key] = canonical_app_revision(result[key])
    return result


def canonical_app_revision(value: Any) -> str:
    """版本仅接受有时区时间，归一到 UTC；拒绝数字时间戳和 naive 时间。"""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("应用版本必须为带时区时间")
    return value.astimezone(UTC).isoformat(timespec="microseconds")
