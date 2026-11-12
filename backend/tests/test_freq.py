"""验证应用频控覆盖的优先级和数值边界；计数行为由账本测试覆盖。"""

from __future__ import annotations

import pytest

from app.services.freq import FrequencyLimits


def test_frequency_limits_apply_app_override_without_changing_other_limits() -> None:
    limits = FrequencyLimits.from_config(
        verify_per_minute=2,
        verify_per_day=10,
        market_per_day=1,
        override={"market_per_day": 3},
    )

    assert limits == FrequencyLimits(verify_per_minute=2, verify_per_day=10, market_per_day=3)


@pytest.mark.parametrize("field", ["verify_per_minute", "verify_per_day", "market_per_day"])
@pytest.mark.parametrize("value", [0, -1, True])
def test_frequency_limits_reject_invalid_app_override(field: str, value: int) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        FrequencyLimits.from_config(
            verify_per_minute=1,
            verify_per_day=10,
            market_per_day=1,
            override={field: value},
        )
