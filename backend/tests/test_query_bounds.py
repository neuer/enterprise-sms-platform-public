import pytest

from app.core.query_bounds import SIGNED_BIGINT_MAX, ensure_bigint_id, ensure_page_offset


def test_page_offset_rejects_values_that_cannot_bind_as_bigint() -> None:
    ensure_page_offset(1, 1)
    ensure_page_offset(1_000_000, 100)
    with pytest.raises(ValueError, match="out of bounds"):
        ensure_page_offset(9_223_372_036_854_775_809, 1)
    with pytest.raises(ValueError, match="out of bounds"):
        ensure_page_offset(1_000_001, 1)
    with pytest.raises(ValueError, match="out of bounds"):
        ensure_page_offset(0, 1)
    with pytest.raises(ValueError, match="out of bounds"):
        ensure_page_offset(True, 1)  # type: ignore[arg-type]


def test_bigint_id_keeps_values_above_int32_and_rejects_unsigned_overflow() -> None:
    ensure_bigint_id(2_147_483_648)
    ensure_bigint_id(SIGNED_BIGINT_MAX)
    ensure_bigint_id(None)
    with pytest.raises(ValueError, match="app_id"):
        ensure_bigint_id(SIGNED_BIGINT_MAX + 1)
    with pytest.raises(ValueError, match="app_id"):
        ensure_bigint_id(0)
