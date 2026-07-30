from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.crypto import CryptoService


def _crypto() -> CryptoService:
    return CryptoService(
        aes_keys={1: b"a" * 32, 2: b"b" * 32},
        hmac_keys={1: b"c" * 32, 2: b"d" * 32},
        active_version=2,
    )


def _write_allowlist(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}) + "\n",
        encoding="utf-8",
    )


def test_guard_accepts_active_and_previous_hmac_key_versions(tmp_path: Path) -> None:
    from app.services.vendor_test_guard import VendorTestRecipientGuard

    crypto = _crypto()
    first = "13900000001"
    second = "13900000002"
    allowlist = tmp_path / "allowlist.json"
    _write_allowlist(
        allowlist,
        [
            {"key_version": 1, "phone_hmac": crypto.phone_hmac(first, 1)},
            {"key_version": 2, "phone_hmac": crypto.phone_hmac(second, 2)},
        ],
    )

    guard = VendorTestRecipientGuard.load(allowlist, crypto)

    guard.require_allowed([first, second])


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"entries":[],"phone":"13900000001"}',
        '{"schema_version":1,"schema_version":1,"entries":[]}',
        (
            '{"schema_version":1,"entries":['
            '{"key_version":1,"phone_hmac":"'
            + "a" * 64
            + '","phone":"13900000001"}]}'
        ),
        (
            '{"schema_version":1,"entries":['
            '{"key_version":1,"phone_hmac":"'
            + "a" * 64
            + '"},{"key_version":1,"phone_hmac":"'
            + "a" * 64
            + '"}]}'
        ),
        '{"schema_version":1,"entries":[{"key_version":true,"phone_hmac":"bad"}]}',
    ],
)
def test_allowlist_rejects_unknown_fields_plain_phone_duplicates_and_invalid_values(
    tmp_path: Path,
    raw: str,
) -> None:
    from app.services.vendor_test_guard import VendorTestAllowlistError, VendorTestRecipientGuard

    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(raw, encoding="utf-8")

    with pytest.raises(VendorTestAllowlistError):
        VendorTestRecipientGuard.load(allowlist, _crypto())


def test_guard_rejects_unregistered_phone_without_exposing_phone_or_hmac(
    tmp_path: Path,
) -> None:
    from app.services.vendor_test_guard import VendorTestRecipientDenied, VendorTestRecipientGuard

    crypto = _crypto()
    registered = "13900000001"
    denied = "13900000002"
    allowlist = tmp_path / "allowlist.json"
    _write_allowlist(
        allowlist,
        [{"key_version": 2, "phone_hmac": crypto.phone_hmac(registered)}],
    )
    guard = VendorTestRecipientGuard.load(allowlist, crypto)

    with pytest.raises(VendorTestRecipientDenied) as captured:
        guard.require_allowed([registered, denied])

    message = str(captured.value)
    assert captured.value.denied_count == 1
    assert denied not in message
    assert crypto.phone_hmac(denied) not in message


def test_guard_reloads_atomically_replaced_allowlist_without_restart(tmp_path: Path) -> None:
    from app.services.vendor_test_guard import VendorTestRecipientDenied, VendorTestRecipientGuard

    crypto = _crypto()
    phone = "13900000001"
    allowlist = tmp_path / "allowlist.json"
    _write_allowlist(
        allowlist,
        [{"key_version": 2, "phone_hmac": crypto.phone_hmac(phone)}],
    )
    guard = VendorTestRecipientGuard.load(allowlist, crypto)
    guard.require_allowed([phone])

    replacement = tmp_path / "replacement.json"
    _write_allowlist(replacement, [])
    replacement.replace(allowlist)

    with pytest.raises(VendorTestRecipientDenied):
        guard.require_allowed([phone])


def test_allowlist_rejects_missing_or_oversized_file(tmp_path: Path) -> None:
    from app.services.vendor_test_guard import VendorTestAllowlistError, VendorTestRecipientGuard

    with pytest.raises(VendorTestAllowlistError, match="unavailable"):
        VendorTestRecipientGuard.load(tmp_path / "missing.json", _crypto())

    oversized = tmp_path / "oversized.json"
    oversized.write_text(" " * 1_048_577, encoding="utf-8")
    with pytest.raises(VendorTestAllowlistError, match="too large"):
        VendorTestRecipientGuard.load(oversized, _crypto())


def test_combined_live_guard_checks_control_state_before_recipient_data() -> None:
    from app.services.vendor_control_state import VendorControlStateUnavailable
    from app.services.vendor_test_guard import VendorLiveTestGuard

    events: list[str] = []

    class State:
        def require_fresh(self) -> None:
            events.append("state")
            raise VendorControlStateUnavailable(
                "真实联调控制状态不可用",
                requires_critical_pause=True,
            )

    class Recipients:
        def require_allowed(self, phones: object) -> None:
            events.append("recipients")

    guard = VendorLiveTestGuard(State(), Recipients())

    with pytest.raises(VendorControlStateUnavailable):
        guard.require_send_allowed(["13900000001"])

    assert events == ["state"]
