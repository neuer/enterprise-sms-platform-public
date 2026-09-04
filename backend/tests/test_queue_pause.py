from __future__ import annotations

from app.services.queue_pause import parse_queue_pause_claim


def test_parse_queue_pause_claim_strips_generation_from_vendor_codes() -> None:
    assert parse_queue_pause_claim(None) is None
    assert parse_queue_pause_claim("") is None
    assert parse_queue_pause_claim("999") == "999"
    assert parse_queue_pause_claim("999:4") == "999"
    assert parse_queue_pause_claim("1000:12") == "1000"
    assert parse_queue_pause_claim("vendor-test-manual") == "vendor-test-manual"
    assert parse_queue_pause_claim("vendor-test-agent-stale") == (
        "vendor-test-agent-stale"
    )
