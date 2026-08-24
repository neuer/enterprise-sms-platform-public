"""T5-04：capture / parse / eligibility 分离与故障矩阵。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from app.core.auth.accounts import SecurityPrincipal
from app.services.crypto import EncryptionContext
from app.services.raw_parse import (
    ELIGIBILITY_AUTOMATIC,
    ELIGIBILITY_MANUAL,
    ELIGIBILITY_NEVER,
    PARSE_PROCESSED,
    PARSE_PROTOCOL_INVALID,
    PARSE_TRANSIENT_FAILURE,
    PARSE_UNATTEMPTED,
    RAW_PARSER_VERSION,
    auto_replay_allowed,
    classify_raw_disposition,
    historical_sql_case,
    mark_error_column_values,
    persist_column_values,
    reevaluate_disposition,
    reevaluate_forbidden_message,
)
from app.services.raw_replay import (
    RawReplayConflict,
    RawReplayNotFound,
    RawReplayRecord,
    RawReplayService,
)
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    CAPTURE_UNKNOWN_LEGACY,
)
from app.vendor.zhihui import VendorApiError, VendorProtocolError

ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")
BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS = BACKEND / "migrations/versions"


def _normalize_sql(value: str) -> str:
    return " ".join(value.split())


class FakeCrypto:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def decrypt_bound_bytes(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = True,
    ) -> bytes:
        return self.raw


class FakeReevaluateRepository:
    def __init__(self, record: RawReplayRecord | None) -> None:
        self.record = record
        self.updates: list[dict[str, object]] = []
        self.audits: list[dict[str, object]] = []
        self.claim_calls: list[tuple[int, bool]] = []

    async def claim_raw_for_replay(
        self, raw_id: int, *, allow_manual: bool = True
    ) -> object:
        self.claim_calls.append((raw_id, allow_manual))
        if self.record is None:
            return None
        return type("Claim", (), {"record": self.record, "claimed": True})()

    async def mark_replay_error(self, raw_id: int, error: str, **_: object) -> None:
        return None

    async def load_raw_for_reevaluate(self, raw_id: int) -> RawReplayRecord | None:
        return self.record

    async def apply_raw_reevaluation(
        self,
        raw_id: int,
        *,
        expected_processed: bool,
        expected_parse_state: str,
        expected_eligibility: str,
        disposition: object,
        error: str | None,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self.updates.append(
            {
                "raw_id": raw_id,
                "parse_state": getattr(disposition, "parse_state"),
                "replay_eligibility": getattr(disposition, "replay_eligibility"),
                "error": error,
            }
        )
        self.audits.append(
            {
                "raw_id": raw_id,
                "actor": actor,
                "ip": ip,
                "account_id": principal.account_id,
                "after": after,
                "before": before,
            }
        )

    async def has_human_raw_replay_audit(self, raw_id: int) -> bool:
        return False

    async def audit_raw_replay(self, raw_id: int, **_: object) -> None:
        return None


def test_http_and_encoding_persist_never_and_skip_automatic() -> None:
    http = persist_column_values(
        capture_state=CAPTURE_COMPLETE, http_status=500, content_encoding="identity"
    )
    encoding = persist_column_values(
        capture_state=CAPTURE_COMPLETE, http_status=200, content_encoding="unsupported"
    )
    assert http == {
        "parse_state": PARSE_PROTOCOL_INVALID,
        "replay_eligibility": ELIGIBILITY_NEVER,
    }
    assert encoding == {
        "parse_state": PARSE_PROTOCOL_INVALID,
        "replay_eligibility": ELIGIBILITY_NEVER,
    }
    assert auto_replay_allowed(replay_eligibility=http["replay_eligibility"]) is False
    assert auto_replay_allowed(replay_eligibility=encoding["replay_eligibility"]) is False


def test_complete_identity_success_stays_automatic_for_process_kill() -> None:
    columns = persist_column_values(
        capture_state=CAPTURE_COMPLETE, http_status=200, content_encoding="identity"
    )
    assert columns == {
        "parse_state": PARSE_UNATTEMPTED,
        "replay_eligibility": ELIGIBILITY_AUTOMATIC,
    }
    assert (
        auto_replay_allowed(
            replay_eligibility=ELIGIBILITY_AUTOMATIC,
            capture_state=CAPTURE_COMPLETE,
            processed=False,
            replay_attempts=0,
        )
        is True
    )


@pytest.mark.parametrize(
    ("error", "exc", "parse_state", "eligibility"),
    [
        (
            "VendorProtocolError: vendor response is not JSON",
            VendorProtocolError("vendor response is not JSON"),
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_MANUAL,
        ),
        (
            "VendorProtocolError: vendor response envelope is invalid",
            VendorProtocolError("vendor response envelope is invalid"),
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_MANUAL,
        ),
        (
            "VendorApiError: vendor response parsing failed",
            VendorApiError(999),
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_NEVER,
        ),
        (
            "skipped 2 invalid report items",
            None,
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_MANUAL,
        ),
        (
            "ValueError: GetReport.data must be an object array",
            ValueError("GetReport.data must be an object array"),
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_MANUAL,
        ),
        (
            "OperationalError: server closed the connection",
            OperationalError("server closed the connection", None, None),
            PARSE_TRANSIENT_FAILURE,
            ELIGIBILITY_AUTOMATIC,
        ),
        (
            "LockNotAvailableError: lock timeout",
            None,
            PARSE_TRANSIENT_FAILURE,
            ELIGIBILITY_AUTOMATIC,
        ),
        (
            "CancelledError: task cancelled",
            None,
            PARSE_TRANSIENT_FAILURE,
            ELIGIBILITY_AUTOMATIC,
        ),
        (
            "raw payload integrity mismatch",
            None,
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_NEVER,
        ),
    ],
)
def test_error_matrix_separates_manual_never_and_automatic(
    error: str,
    exc: BaseException | None,
    parse_state: str,
    eligibility: str,
) -> None:
    disposition = classify_raw_disposition(error=error, exc=exc)
    assert disposition.parse_state == parse_state
    assert disposition.replay_eligibility == eligibility
    assert mark_error_column_values(error, exc) == {
        "parse_state": parse_state,
        "replay_eligibility": eligibility,
    }
    assert auto_replay_allowed(replay_eligibility=eligibility) is (
        eligibility == ELIGIBILITY_AUTOMATIC
    )


def test_deterministic_json_does_not_remain_automatic_until_attempt_cap() -> None:
    columns = mark_error_column_values(
        "VendorProtocolError: vendor response parsing failed",
        VendorProtocolError("vendor response parsing failed"),
    )
    assert columns["replay_eligibility"] == ELIGIBILITY_MANUAL
    assert (
        auto_replay_allowed(
            replay_eligibility=columns["replay_eligibility"],
            replay_attempts=0,
            max_attempts=10,
        )
        is False
    )
    assert (
        auto_replay_allowed(
            replay_eligibility=columns["replay_eligibility"],
            replay_attempts=9,
            max_attempts=10,
        )
        is False
    )


def test_historical_unclassifiable_rows_are_conservative() -> None:
    unknown = classify_raw_disposition(
        capture_state=CAPTURE_COMPLETE,
        http_status=200,
        content_encoding="identity",
        error="legacy boom without class",
        historical=True,
    )
    missing_http = classify_raw_disposition(
        capture_state=CAPTURE_COMPLETE,
        error=None,
        historical=True,
    )
    crash_before_parse = classify_raw_disposition(
        capture_state=CAPTURE_COMPLETE,
        http_status=200,
        content_encoding="identity",
        historical=True,
    )
    assert unknown.replay_eligibility == ELIGIBILITY_MANUAL
    assert missing_http.replay_eligibility == ELIGIBILITY_MANUAL
    assert crash_before_parse.replay_eligibility == ELIGIBILITY_AUTOMATIC
    assert unknown.reason == "unclassifiable"


def test_capture_bans_remain_never() -> None:
    for state in (
        CAPTURE_TRUNCATED,
        CAPTURE_PROTOCOL_INVALID,
        CAPTURE_UNKNOWN_LEGACY,
    ):
        disposition = classify_raw_disposition(capture_state=state)
        assert disposition.replay_eligibility == ELIGIBILITY_NEVER
        assert reevaluate_forbidden_message(state) is not None
    oversized = classify_raw_disposition(capture_state=CAPTURE_COMPLETE_TOO_LARGE)
    assert oversized == classify_raw_disposition(capture_state=CAPTURE_COMPLETE_TOO_LARGE)
    assert oversized.replay_eligibility == ELIGIBILITY_MANUAL
    assert oversized.parse_state == PARSE_UNATTEMPTED


def test_processed_is_never_and_not_automatic() -> None:
    disposition = classify_raw_disposition(processed=True)
    assert disposition.parse_state == PARSE_PROCESSED
    assert disposition.replay_eligibility == ELIGIBILITY_NEVER
    assert auto_replay_allowed(replay_eligibility=ELIGIBILITY_NEVER, processed=True) is False


def test_migration_0075_uses_historical_sql_case() -> None:
    parse_sql, eligibility_sql = historical_sql_case()
    source = (MIGRATIONS / "0075_raw_parse_eligibility.py").read_text(encoding="utf-8")
    assert 'revision = "0075_raw_parse_eligibility"' in source
    assert 'down_revision = "0074_raw_legacy_capture"' in source
    assert _normalize_sql(parse_sql) in _normalize_sql(source)
    assert _normalize_sql(eligibility_sql) in _normalize_sql(source)
    assert "DEFAULT 'manual'" in source


@pytest.mark.asyncio
async def test_system_replay_refuses_manual_and_never() -> None:
    for eligibility in (ELIGIBILITY_MANUAL, ELIGIBILITY_NEVER):
        repository = FakeReevaluateRepository(
            RawReplayRecord(
                9,
                "report",
                b"cipher",
                "a" * 64,
                1,
                False,
                replay_eligibility=eligibility,
            )
        )
        service = RawReplayService(
            repository,  # type: ignore[arg-type]
            FakeCrypto(b"{}"),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
        with pytest.raises(RawReplayConflict, match="不具备自动重放|禁止重放"):
            await service.replay(
                9,
                actor="system-reconcile",
                ip="127.0.0.1",
                system_producer=True,
            )
        assert repository.claim_calls == [(9, False)]


@pytest.mark.asyncio
async def test_parser_reevaluate_promotes_manual_json_after_successful_decode() -> None:
    raw = json.dumps({"code": 0, "msg": "ok", "data": []}).encode()
    repository = FakeReevaluateRepository(
        RawReplayRecord(
            11,
            "report",
            b"cipher",
            hashlib.sha256(raw).hexdigest(),
            1,
            False,
            200,
            "identity",
            CAPTURE_COMPLETE,
            0,
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_MANUAL,
            "VendorProtocolError: vendor response is not JSON",
        )
    )
    service = RawReplayService(
        repository,  # type: ignore[arg-type]
        FakeCrypto(raw),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    disposition = await service.reevaluate(
        11, actor="admin01", ip="10.0.0.8", principal=ADMIN
    )

    assert disposition.parse_state == PARSE_UNATTEMPTED
    assert disposition.replay_eligibility == ELIGIBILITY_AUTOMATIC
    assert repository.updates == [
        {
            "raw_id": 11,
            "parse_state": PARSE_UNATTEMPTED,
            "replay_eligibility": ELIGIBILITY_AUTOMATIC,
            "error": None,
        }
    ]
    assert repository.audits[0]["after"]["parser_version"] == RAW_PARSER_VERSION
    assert repository.audits[0]["account_id"] == 1
    assert "phone" not in str(repository.audits).casefold()


@pytest.mark.asyncio
async def test_parser_reevaluate_keeps_http_never_and_refuses_truncated() -> None:
    raw = b'{"code":0,"msg":"ok","data":[]}'
    http_repo = FakeReevaluateRepository(
        RawReplayRecord(
            12,
            "report",
            b"cipher",
            hashlib.sha256(raw).hexdigest(),
            1,
            False,
            500,
            "identity",
            CAPTURE_COMPLETE,
            0,
            PARSE_PROTOCOL_INVALID,
            ELIGIBILITY_NEVER,
            "VendorProtocolError: vendor HTTP status 500",
        )
    )
    service = RawReplayService(
        http_repo,  # type: ignore[arg-type]
        FakeCrypto(raw),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    disposition = await service.reevaluate(
        12, actor="admin01", ip="10.0.0.8", principal=ADMIN
    )
    assert disposition.replay_eligibility == ELIGIBILITY_NEVER
    assert disposition.reason == "http_status"
    assert http_repo.updates[0]["error"] == "VendorProtocolError: vendor HTTP status 500"

    truncated = FakeReevaluateRepository(
        RawReplayRecord(
            13,
            "report",
            b"cipher",
            "b" * 64,
            1,
            False,
            capture_state=CAPTURE_TRUNCATED,
            replay_eligibility=ELIGIBILITY_NEVER,
        )
    )
    blocked = RawReplayService(
        truncated,  # type: ignore[arg-type]
        FakeCrypto(b""),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    with pytest.raises(RawReplayConflict, match="不得重新评估"):
        await blocked.reevaluate(13, actor="admin01", ip="10.0.0.8", principal=ADMIN)
    assert truncated.updates == []


@pytest.mark.asyncio
async def test_reevaluate_missing_raw_is_not_found() -> None:
    service = RawReplayService(
        FakeReevaluateRepository(None),  # type: ignore[arg-type]
        FakeCrypto(b""),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    with pytest.raises(RawReplayNotFound):
        await service.reevaluate(99, actor="admin01", ip="10.0.0.8", principal=ADMIN)


@pytest.mark.asyncio
async def test_parser_reevaluate_keeps_processed_never() -> None:
    raw = b'{"code":0,"msg":"ok","data":[]}'
    repository = FakeReevaluateRepository(
        RawReplayRecord(
            14,
            "report",
            b"cipher",
            hashlib.sha256(raw).hexdigest(),
            1,
            True,
            200,
            "identity",
            CAPTURE_COMPLETE,
            1,
            PARSE_PROCESSED,
            ELIGIBILITY_NEVER,
            None,
        )
    )
    service = RawReplayService(
        repository,  # type: ignore[arg-type]
        FakeCrypto(raw),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    disposition = await service.reevaluate(
        14, actor="admin01", ip="10.0.0.8", principal=ADMIN
    )

    assert disposition.parse_state == PARSE_PROCESSED
    assert disposition.replay_eligibility == ELIGIBILITY_NEVER
    assert repository.updates == [
        {
            "raw_id": 14,
            "parse_state": PARSE_PROCESSED,
            "replay_eligibility": ELIGIBILITY_NEVER,
            "error": None,
        }
    ]
    assert repository.audits[0]["after"]["parse_state"] == PARSE_PROCESSED
    assert repository.audits[0]["after"]["replay_eligibility"] == ELIGIBILITY_NEVER


def test_reevaluate_disposition_does_not_project_business_success() -> None:
    promoted = reevaluate_disposition(
        capture_state=CAPTURE_COMPLETE,
        http_status=200,
        content_encoding="identity",
        processed=False,
        decoded_ok=True,
    )
    assert promoted.parse_state == PARSE_UNATTEMPTED
    assert promoted.replay_eligibility == ELIGIBILITY_AUTOMATIC
    oversized = reevaluate_disposition(
        capture_state=CAPTURE_COMPLETE_TOO_LARGE,
        http_status=200,
        content_encoding="identity",
        processed=False,
        decoded_ok=True,
    )
    assert oversized.replay_eligibility == ELIGIBILITY_MANUAL
    processed = reevaluate_disposition(
        capture_state=CAPTURE_COMPLETE,
        http_status=200,
        content_encoding="identity",
        processed=True,
        decoded_ok=True,
    )
    assert processed.parse_state == PARSE_PROCESSED
    assert processed.replay_eligibility == ELIGIBILITY_NEVER
