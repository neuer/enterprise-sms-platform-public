"""事实账本号码频控共用的配置值；计数仅由 UsageLedgerService 维护。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrequencyLimits:
    verify_per_minute: int = 1
    verify_per_day: int = 10
    market_per_day: int = 1

    @classmethod
    def from_config(
        cls,
        *,
        verify_per_minute: int,
        verify_per_day: int,
        market_per_day: int,
        override: Mapping[str, int] | None,
    ) -> FrequencyLimits:
        values = {
            "verify_per_minute": verify_per_minute,
            "verify_per_day": verify_per_day,
            "market_per_day": market_per_day,
        }
        if override:
            values.update({key: value for key, value in override.items() if key in values})
        invalid = any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in values.values()
        )
        if invalid:
            raise ValueError("frequency limits must be positive integers")
        return cls(**values)
