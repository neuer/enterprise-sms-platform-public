from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from app.services.crypto import CryptoService
from app.services.vendor_event_audit import (
    LegacyReplyFact,
    find_legacy_reply_duplicates,
)


def _crypto() -> CryptoService:
    first = base64.b64encode(b"a" * 32).decode()
    second = base64.b64encode(b"b" * 32).decode()
    ring = json.dumps({"active_version": 2, "keys": {"1": first, "2": second}})
    return CryptoService.from_secret_values(ring, ring)


def _fact(
    crypto: CryptoService,
    *,
    event_key: str,
    version: int,
    created_at: datetime,
) -> LegacyReplyFact:
    phone = "13800138000"
    protected = crypto.protect_phone(phone, table="reply_event")
    if version != protected.key_version:
        first = base64.b64encode(b"a" * 32).decode()
        v1 = CryptoService.from_secret_values(first, first)
        protected = v1.protect_phone(phone, table="reply_event")
    return LegacyReplyFact(
        event_key=event_key,
        vendor_task_id="task-1",
        custom_id=None,
        phone_enc=protected.phone_enc,
        phone_hmac=protected.phone_hmac,
        phone_mask="138****8000",
        key_version=version,
        content="TD",
        reply_time=datetime(2026, 7, 15, 0, 30, tzinfo=UTC),
        created_at=created_at,
    )


def test_duplicate_audit_groups_cross_version_without_returning_phone_or_hmac() -> None:
    crypto = _crypto()
    created_at = datetime(2026, 7, 15, tzinfo=UTC)
    groups = find_legacy_reply_duplicates(
        [
            _fact(crypto, event_key="1" * 64, version=1, created_at=created_at),
            _fact(
                crypto,
                event_key="2" * 64,
                version=2,
                created_at=created_at + timedelta(seconds=1),
            ),
        ],
        crypto,
    )

    assert len(groups) == 1
    payload = groups[0].safe_json()
    assert payload["keep_event_key"] == "1" * 64
    assert payload["duplicate_event_keys"] == ["2" * 64]
    assert payload["phone_masks"] == ["138****8000"]
    assert "13800138000" not in str(payload)
    assert crypto.phone_hmac("13800138000", 1) not in str(payload)
    assert crypto.phone_hmac("13800138000", 2) not in str(payload)
