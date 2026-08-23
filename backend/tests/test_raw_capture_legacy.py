"""0074 历史 raw 分类、重放资格与只读盘点。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.raw_capture_legacy import (
    AUTO_PARSE_LIMIT_BYTES,
    BOUND_ENVELOPE_MAGIC,
    BOUND_ENVELOPE_OVERHEAD_BYTES,
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    CAPTURE_UNKNOWN_LEGACY,
    FORBIDDEN_INVENTORY_KEYS,
    LEGACY_ENVELOPE_OVERHEAD_BYTES,
    RECOVERY_CAPTURE_LIMIT_BYTES,
    RawInventoryInput,
    RawLegacyEvidence,
    build_inventory,
    classify_historical_raw,
    inventory_leak_reasons,
    needs_complete_reclassify,
    replay_eligibility,
    replay_forbidden_message,
)
from app.services.raw_replay import RawReplayConflict, RawReplayRecord, RawReplayService
from app.services.raw_spill import (
    VALID_CAPTURE_STATES,
    is_non_replayable_capture,
    normalize_capture_state,
)
from scripts_support.inventory_raw_capture_legacy import render_inventory

ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
MIGRATIONS = BACKEND / "migrations/versions"


def _sme2(plaintext_len: int) -> tuple[int, bytes]:
    return plaintext_len + BOUND_ENVELOPE_OVERHEAD_BYTES, BOUND_ENVELOPE_MAGIC


def _legacy(plaintext_len: int) -> tuple[int, bytes]:
    return plaintext_len + LEGACY_ENVELOPE_OVERHEAD_BYTES, b"\x00\x01\x02\x03"


def _evidence(
    plaintext_len: int,
    *,
    error: str | None = None,
    processed: bool = False,
    sme2: bool = True,
) -> RawLegacyEvidence:
    enc_len, prefix = _sme2(plaintext_len) if sme2 else _legacy(plaintext_len)
    return RawLegacyEvidence(
        enc_len=enc_len,
        payload_prefix=prefix,
        error=error,
        processed=processed,
    )


MIGRATION_FIXTURES = (
    {
        "name": "processed_ordinary_complete",
        "evidence": _evidence(800, processed=True),
        "after_state": CAPTURE_COMPLETE,
        "before_auto": False,
        "before_ops": False,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unprocessed_ordinary_complete",
        "evidence": _evidence(800, processed=False),
        "after_state": CAPTURE_COMPLETE,
        "before_auto": True,
        "before_ops": True,
        "after_auto": True,
        "after_ops": True,
    },
    {
        "name": "processed_oversized_complete",
        "evidence": _evidence(
            AUTO_PARSE_LIMIT_BYTES + 1,
            error="report oversized payload persisted after consume gap",
            processed=True,
        ),
        "after_state": CAPTURE_COMPLETE_TOO_LARGE,
        "before_auto": False,
        "before_ops": False,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unprocessed_oversized_complete",
        "evidence": _evidence(
            AUTO_PARSE_LIMIT_BYTES + 4096,
            error="vendor response exceeds automatic processing limit",
            processed=False,
        ),
        "after_state": CAPTURE_COMPLETE_TOO_LARGE,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": True,
    },
    {
        "name": "processed_truncated",
        "evidence": _evidence(
            AUTO_PARSE_LIMIT_BYTES,
            error="report truncated vendor response beyond recovery limit",
            processed=True,
        ),
        "after_state": CAPTURE_TRUNCATED,
        "before_auto": False,
        "before_ops": False,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unprocessed_truncated",
        "evidence": _evidence(
            RECOVERY_CAPTURE_LIMIT_BYTES - 1,
            error="reply truncated vendor response beyond recovery limit",
            processed=False,
        ),
        "after_state": CAPTURE_TRUNCATED,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unprocessed_truncated_by_size",
        "evidence": _evidence(RECOVERY_CAPTURE_LIMIT_BYTES + 1, processed=False),
        "after_state": CAPTURE_TRUNCATED,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unclassifiable_invalid_envelope",
        "evidence": RawLegacyEvidence(enc_len=8, payload_prefix=b"xx", processed=False),
        "after_state": CAPTURE_UNKNOWN_LEGACY,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unclassifiable_exact_4mib_unprocessed",
        "evidence": _evidence(AUTO_PARSE_LIMIT_BYTES, processed=False),
        "after_state": CAPTURE_UNKNOWN_LEGACY,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unclassifiable_exact_64mib",
        "evidence": _evidence(RECOVERY_CAPTURE_LIMIT_BYTES, processed=False),
        "after_state": CAPTURE_UNKNOWN_LEGACY,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": False,
    },
    {
        "name": "unclassifiable_conflicting_error_and_size",
        "evidence": _evidence(
            RECOVERY_CAPTURE_LIMIT_BYTES + 8,
            error="report oversized payload persisted after consume gap",
            processed=False,
        ),
        "after_state": CAPTURE_UNKNOWN_LEGACY,
        "before_auto": True,
        "before_ops": True,
        "after_auto": False,
        "after_ops": False,
    },
)


@pytest.mark.parametrize("fixture", MIGRATION_FIXTURES, ids=lambda item: str(item["name"]))
def test_migration_fixtures_classify_and_assert_replay_eligibility(
    fixture: dict[str, object],
) -> None:
    evidence = fixture["evidence"]
    assert isinstance(evidence, RawLegacyEvidence)
    before = replay_eligibility(None, processed=evidence.processed)
    after_state = classify_historical_raw(evidence)
    after = replay_eligibility(after_state, processed=evidence.processed)

    assert after_state == fixture["after_state"]
    assert before.auto is fixture["before_auto"]
    assert before.ops is fixture["before_ops"]
    assert after.auto is fixture["after_auto"]
    assert after.ops is fixture["after_ops"]
    if after_state == CAPTURE_COMPLETE and not evidence.processed:
        assert needs_complete_reclassify(CAPTURE_COMPLETE, evidence) is False
    if after_state != CAPTURE_COMPLETE:
        assert needs_complete_reclassify(CAPTURE_COMPLETE, evidence) is True


def test_processed_exact_4mib_is_ordinary_complete() -> None:
    evidence = _evidence(AUTO_PARSE_LIMIT_BYTES, processed=True)
    assert classify_historical_raw(evidence) == CAPTURE_COMPLETE
    assert replay_eligibility(CAPTURE_COMPLETE, processed=True) == replay_eligibility(
        None, processed=True
    )


def test_legacy_envelope_oversized_is_not_auto_replayable() -> None:
    evidence = _evidence(
        AUTO_PARSE_LIMIT_BYTES + 10,
        processed=False,
        sme2=False,
    )
    assert classify_historical_raw(evidence) == CAPTURE_COMPLETE_TOO_LARGE
    eligibility = replay_eligibility(CAPTURE_COMPLETE_TOO_LARGE, processed=False)
    assert eligibility.auto is False and eligibility.ops is True


def test_empty_schema_default_remains_complete_for_new_rows() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "capture_state  VARCHAR(24) NOT NULL DEFAULT 'complete'" in schema
    assert "'unknown_legacy'" in schema
    assert "'protocol_invalid'" in schema
    source = (MIGRATIONS / "0072_raw_capture_state.py").read_text(encoding="utf-8")
    assert "DEFAULT 'complete'" in source
    assert "DEFAULT 'unknown_legacy'" not in source
    assert "历史行都是完整落库路径" in source


def test_migrations_keep_classification_constants_and_repair_old_0072() -> None:
    shipped = (MIGRATIONS / "0072_raw_capture_state.py").read_text(encoding="utf-8")
    protocol = (MIGRATIONS / "0073_raw_protocol_invalid.py").read_text(encoding="utf-8")
    repair = (MIGRATIONS / "0074_raw_legacy_capture.py").read_text(encoding="utf-8")
    assert "unknown_legacy" not in shipped
    assert "protocol_invalid" in protocol
    assert "unknown_legacy" not in protocol
    assert 'down_revision = "0072_raw_capture_state"' in protocol
    assert str(AUTO_PARSE_LIMIT_BYTES) in repair
    assert str(RECOVERY_CAPTURE_LIMIT_BYTES) in repair
    assert "unknown_legacy" in repair
    assert "protocol_invalid" in repair
    assert "decrypt" not in repair.casefold()
    assert "payload_enc" in repair
    assert "phone" not in repair.casefold()
    assert "WHERE capture_state='complete'" in repair
    assert "WHERE capture_state='protocol_invalid'" not in repair
    assert 'down_revision = "0073_raw_protocol_invalid"' in repair
    assert 'revision = "0074_raw_legacy_capture"' in repair


def test_inventory_is_aggregate_only_and_rejects_poisoned_fields() -> None:
    window = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
    rows = [
        RawInventoryInput(
            source="report",
            processed=False,
            enc_len=_sme2(100)[0],
            payload_prefix=BOUND_ENVELOPE_MAGIC,
            error=None,
            fetched_at=window,
            capture_state=None,
            raw_id=1,
        ),
        RawInventoryInput(
            source="reply",
            processed=False,
            enc_len=_sme2(AUTO_PARSE_LIMIT_BYTES + 1)[0],
            payload_prefix=BOUND_ENVELOPE_MAGIC,
            error="report oversized payload persisted after consume gap",
            fetched_at=window,
            capture_state=None,
            raw_id=2,
        ),
        RawInventoryInput(
            source="report",
            processed=False,
            enc_len=4,
            payload_prefix=b"xx",
            error="keep 13800138000 out of inventory",
            fetched_at=window,
            capture_state=None,
            raw_id=3,
        ),
    ]
    document = render_inventory(rows)
    assert document["row_count"] == 3
    assert document["by_capture_state"][CAPTURE_COMPLETE] == 1
    assert document["by_capture_state"][CAPTURE_COMPLETE_TOO_LARGE] == 1
    assert document["by_capture_state"][CAPTURE_UNKNOWN_LEGACY] == 1
    assert document["by_replay_eligibility"]["auto"] == 1
    assert document["by_replay_eligibility"]["ops_only"] == 1
    assert document["by_replay_eligibility"]["forbidden"] == 1
    assert document["windows"]["intermediate_413_415"]["in_413_415"] == 3
    assert inventory_leak_reasons(document) == []
    serialized = str(document)
    assert "13800138000" not in serialized
    assert "payload_enc" not in serialized
    assert "oversized payload persisted" not in serialized
    for key in FORBIDDEN_INVENTORY_KEYS:
        assert key not in document


def test_inventory_rejects_document_with_ciphertext_or_phone_keys() -> None:
    leaked = build_inventory([])
    poisoned = dict(leaked)
    poisoned["payload_enc"] = "cipher"
    assert "forbidden_key:payload_enc" in inventory_leak_reasons(poisoned)


class _ForbiddenReplayRepository:
    def __init__(self, capture_state: str) -> None:
        self.capture_state = capture_state

    async def claim_raw_for_replay(self, raw_id: int, **_: object) -> object:
        return type(
            "Claim",
            (),
            {
                "claimed": False,
                "record": RawReplayRecord(
                    raw_id,
                    "report",
                    b"ciphertext",
                    "a" * 64,
                    1,
                    False,
                    capture_state=self.capture_state,
                ),
            },
        )()

    async def mark_replay_error(self, raw_id: int, error: str) -> None:
        return None

    async def has_human_raw_replay_audit(self, raw_id: int) -> bool:
        return True

    async def audit_raw_replay(self, raw_id: int, **_: object) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "message"),
    [
        (CAPTURE_TRUNCATED, "截断"),
        (CAPTURE_PROTOCOL_INVALID, "协议异常"),
        (CAPTURE_UNKNOWN_LEGACY, "未分类历史"),
    ],
)
async def test_forbidden_capture_states_cannot_replay(
    state: str, message: str
) -> None:
    service = RawReplayService(
        _ForbiddenReplayRepository(state),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    with pytest.raises(RawReplayConflict, match=message):
        await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)
    assert replay_forbidden_message(state)


def test_unknown_legacy_is_a_valid_persisted_capture_state() -> None:
    assert CAPTURE_UNKNOWN_LEGACY in VALID_CAPTURE_STATES
    assert CAPTURE_PROTOCOL_INVALID in VALID_CAPTURE_STATES
    assert normalize_capture_state(CAPTURE_UNKNOWN_LEGACY) == CAPTURE_UNKNOWN_LEGACY
    assert is_non_replayable_capture(CAPTURE_UNKNOWN_LEGACY)
    assert is_non_replayable_capture(CAPTURE_PROTOCOL_INVALID)
