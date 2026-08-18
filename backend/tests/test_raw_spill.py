from __future__ import annotations

from pathlib import Path

from app.services.raw_spill import RawSpillStore


def test_spill_write_is_durable_and_round_trips(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    digest = "a" * 64
    payload = b"encrypted-raw"
    path = store.write(
        source="report",
        payload_sha256=digest,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=payload,
    )
    assert path.exists()
    pending = store.list_pending()
    assert len(pending) == 1
    assert pending[0].payload_enc == payload
    assert pending[0].payload_sha256 == digest
    store.remove("report", digest)
    assert store.list_pending() == []
