"""raw 解析面与重放资格：与 capture_state 分离的稳定事实。

capture_state 只描述字节是否完整取得。parse_state 描述解析结果。
replay_eligibility 是自动重放扫描的唯一资格事实：automatic / manual / never。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_PROTOCOL_INVALID,
    NON_REPLAYABLE_CAPTURE_STATES,
    is_non_replayable_capture,
    normalize_capture_state,
)
from app.vendor.zhihui import VendorApiError, VendorProtocolError

PARSE_UNATTEMPTED = "unattempted"
PARSE_TRANSIENT_FAILURE = "transient_failure"
PARSE_PROTOCOL_INVALID = "protocol_invalid"
PARSE_PROCESSED = "processed"
VALID_PARSE_STATES = frozenset(
    {
        PARSE_UNATTEMPTED,
        PARSE_TRANSIENT_FAILURE,
        PARSE_PROTOCOL_INVALID,
        PARSE_PROCESSED,
    }
)

ELIGIBILITY_AUTOMATIC = "automatic"
ELIGIBILITY_MANUAL = "manual"
ELIGIBILITY_NEVER = "never"
VALID_REPLAY_ELIGIBILITIES = frozenset(
    {ELIGIBILITY_AUTOMATIC, ELIGIBILITY_MANUAL, ELIGIBILITY_NEVER}
)

# 解析器合同版本。升级后不得自动重评估存量 protocol_invalid；只允许受审计人工动作。
RAW_PARSER_VERSION = 1

_TRANSIENT_TYPE_NAMES = frozenset(
    {
        "BrokenPipeError",
        "CancelledError",
        "CannotConnectNowError",
        "ConnectionDoesNotExistError",
        "ConnectionError",
        "ConnectionFailureError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "DeadlockDetectedError",
        "IdleInTransactionSessionTimeoutError",
        "InterfaceError",
        "LockNotAvailableError",
        "OperationalError",
        "QueryCanceledError",
        "SerializationError",
        "TimeoutError",
        "TooManyConnectionsError",
    }
)
_TRANSIENT_ERROR_MARKERS = (
    "advisory lock",
    "broken pipe",
    "cancellederror",
    "connection does not exist",
    "connection refused",
    "connection reset",
    "could not connect",
    "could not obtain lock",
    "deadlock",
    "interfaceerror",
    "lease expired",
    "lock not available",
    "lock timeout",
    "locknotavailable",
    "operationalerror",
    "query canceled",
    "serializationerror",
    "server closed the connection",
    "too many connections",
    "timeout",
)
_PROTOCOL_ERROR_MARKERS = (
    "content-encoding is forbidden",
    "duplicate or ambiguous json",
    "envelope is invalid",
    "envelope must be an object",
    "must be an object array",
    "parsing failed",
    "raw vendor envelope is invalid",
    "vendor http status",
    "vendor response content-encoding",
    "vendor response envelope",
    "vendor response is not json",
    "vendor response msg is invalid",
    "vendor response parsing failed",
    "vendorapierror",
    "vendorprotocolerror",
)
_NEVER_ERROR_MARKERS = (
    "content-encoding is forbidden",
    "protocol-invalid vendor response",
    "raw payload integrity mismatch",
    "truncated vendor response",
    "vendor http status",
    "vendor response content-encoding",
    "vendorapierror",
)


@dataclass(frozen=True, slots=True)
class RawParseDisposition:
    """一次分类结果。reason 只含无 PII 的稳定标签。"""

    parse_state: str
    replay_eligibility: str
    reason: str

    @property
    def automatic(self) -> bool:
        return self.replay_eligibility == ELIGIBILITY_AUTOMATIC

    @property
    def never(self) -> bool:
        return self.replay_eligibility == ELIGIBILITY_NEVER


def normalize_parse_state(value: str | None) -> str:
    state = value if value else PARSE_UNATTEMPTED
    if state not in VALID_PARSE_STATES:
        raise ValueError("invalid parse_state")
    return state


def normalize_replay_eligibility(value: str | None) -> str:
    eligibility = value if value else ELIGIBILITY_MANUAL
    if eligibility not in VALID_REPLAY_ELIGIBILITIES:
        raise ValueError("invalid replay_eligibility")
    return eligibility


def _http_success(status: int | None) -> bool | None:
    if status is None:
        return None
    return 200 <= int(status) < 300


def _iter_exception_types(exc: BaseException | None) -> Iterable[type[BaseException]]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield type(current)
        current = current.__cause__ or current.__context__


def is_transient_dependency_error(exc: BaseException | None) -> bool:
    """PostgreSQL、锁、租约等待与进程取消等依赖暂态。"""

    for typ in _iter_exception_types(exc):
        names = {item.__name__ for item in typ.mro()}
        if names & _TRANSIENT_TYPE_NAMES:
            return True
    return False


def _error_text(error: str | None) -> str:
    return (error or "").strip().casefold()


def _error_has_marker(error: str | None, markers: tuple[str, ...]) -> bool:
    text = _error_text(error)
    return bool(text) and any(marker in text for marker in markers)


def classify_raw_disposition(
    *,
    capture_state: str | None = None,
    http_status: int | None = None,
    content_encoding: str | None = None,
    processed: bool = False,
    error: str | None = None,
    exc: BaseException | None = None,
    historical: bool = False,
) -> RawParseDisposition:
    """根据捕获事实、HTTP/编码、异常或历史 error 摘要计算解析面与资格。

    无法可靠判定的历史行必须保守进入 manual/never，不得默认 automatic。
    """

    state = normalize_capture_state(capture_state or CAPTURE_COMPLETE)
    encoding = (content_encoding or "identity").strip().casefold() or "identity"
    if processed:
        return RawParseDisposition(PARSE_PROCESSED, ELIGIBILITY_NEVER, "already_processed")
    if state in NON_REPLAYABLE_CAPTURE_STATES:
        parse_state = (
            PARSE_PROTOCOL_INVALID if state == CAPTURE_PROTOCOL_INVALID else PARSE_UNATTEMPTED
        )
        return RawParseDisposition(parse_state, ELIGIBILITY_NEVER, f"capture_{state}")
    if encoding != "identity":
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, "content_encoding"
        )
    success = _http_success(http_status)
    if success is False:
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, "http_status"
        )
    if state == CAPTURE_COMPLETE_TOO_LARGE:
        return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, "complete_too_large")
    if exc is not None:
        return _classify_exception(exc, error)
    if error:
        return _classify_error_text(error, historical=historical)
    if historical and success is None:
        return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, "unclassifiable")
    return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_AUTOMATIC, "unattempted")


def _classify_exception(
    exc: BaseException, error: str | None
) -> RawParseDisposition:
    if is_transient_dependency_error(exc):
        return RawParseDisposition(
            PARSE_TRANSIENT_FAILURE, ELIGIBILITY_AUTOMATIC, "transient_dependency"
        )
    if isinstance(exc, VendorApiError):
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, "vendor_api_envelope"
        )
    if isinstance(exc, VendorProtocolError):
        message = str(exc)
        if "content-encoding" in message or "HTTP status" in message:
            reason = "content_encoding" if "content-encoding" in message else "http_status"
            return RawParseDisposition(PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, reason)
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_MANUAL, "vendor_protocol"
        )
    if isinstance(exc, (ValueError, TypeError, KeyError, UnicodeError)):
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_MANUAL, type(exc).__name__
        )
    if error:
        return _classify_error_text(error, historical=False)
    return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, type(exc).__name__)


def _classify_error_text(error: str, *, historical: bool) -> RawParseDisposition:
    text = _error_text(error)
    if "truncated vendor response" in text:
        return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_NEVER, "capture_truncated")
    if "protocol-invalid vendor response" in text:
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, "capture_protocol_invalid"
        )
    if "oversized payload" in text:
        return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, "complete_too_large")
    if "raw payload integrity mismatch" in text:
        return RawParseDisposition(PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, "integrity")
    if any(marker in text for marker in _TRANSIENT_ERROR_MARKERS):
        return RawParseDisposition(
            PARSE_TRANSIENT_FAILURE, ELIGIBILITY_AUTOMATIC, "transient_dependency"
        )
    if _error_has_marker(error, _NEVER_ERROR_MARKERS):
        reason = "vendor_api_envelope" if "vendorapierror" in text else "deterministic_wire"
        if "integrity" in text:
            reason = "integrity"
        if "http status" in text:
            reason = "http_status"
        if "content-encoding" in text:
            reason = "content_encoding"
        return RawParseDisposition(PARSE_PROTOCOL_INVALID, ELIGIBILITY_NEVER, reason)
    if _error_has_marker(error, _PROTOCOL_ERROR_MARKERS) or "skipped " in text:
        return RawParseDisposition(
            PARSE_PROTOCOL_INVALID, ELIGIBILITY_MANUAL, "deterministic_parse"
        )
    if historical:
        return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, "unclassifiable")
    return RawParseDisposition(PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, "unclassifiable")


def persist_disposition(
    *,
    capture_state: str | None,
    http_status: int | None,
    content_encoding: str | None,
    processed: bool = False,
) -> RawParseDisposition:
    """落库当时即可判定的资格；JSON/包络错误要等首次解析。"""

    return classify_raw_disposition(
        capture_state=capture_state,
        http_status=http_status,
        content_encoding=content_encoding,
        processed=processed,
    )


def processed_disposition() -> RawParseDisposition:
    return RawParseDisposition(PARSE_PROCESSED, ELIGIBILITY_NEVER, "already_processed")


def reevaluate_forbidden_message(capture_state: str | None) -> str | None:
    """截断或捕获协议异常不得借 parser 升级改写成可自动重放。"""

    if is_non_replayable_capture(capture_state):
        return "截断或协议异常 raw 不得重新评估为可重放"
    return None


def eligibility_conflict_message(
    eligibility: str | None,
    *,
    system_producer: bool,
) -> str | None:
    """系统只认 automatic；never 对自动和人工都禁止。"""

    value = normalize_replay_eligibility(eligibility)
    if value == ELIGIBILITY_NEVER:
        return "该 raw 禁止重放"
    if system_producer and value != ELIGIBILITY_AUTOMATIC:
        return "该 raw 不具备自动重放资格"
    if value not in claim_eligibilities(allow_manual=not system_producer):
        return "该 raw 不具备重放资格"
    return None


def reevaluate_disposition(
    *,
    capture_state: str | None,
    http_status: int | None,
    content_encoding: str | None,
    processed: bool,
    decode_error: BaseException | None = None,
    decoded_ok: bool = False,
) -> RawParseDisposition:
    """parser 升级重评估：先认 HTTP/编码/捕获事实，再认本次解码结果。"""

    if processed:
        return processed_disposition()
    wire = persist_disposition(
        capture_state=capture_state,
        http_status=http_status,
        content_encoding=content_encoding,
        processed=False,
    )
    if wire.never:
        return wire
    if decode_error is not None:
        return classify_raw_disposition(
            capture_state=capture_state,
            http_status=http_status,
            content_encoding=content_encoding,
            exc=decode_error,
        )
    if decoded_ok:
        return wire
    return classify_raw_disposition(
        capture_state=capture_state,
        http_status=http_status,
        content_encoding=content_encoding,
        historical=True,
    )


def auto_replay_allowed(
    *,
    replay_eligibility: str | None,
    capture_state: str | None = None,
    processed: bool = False,
    replay_attempts: int = 0,
    max_attempts: int = 10,
) -> bool:
    """自动扫描只认 persisted eligibility，并保留次数上限。"""

    if processed:
        return False
    if capture_state is not None and normalize_capture_state(capture_state) != CAPTURE_COMPLETE:
        return False
    return (
        normalize_replay_eligibility(replay_eligibility) == ELIGIBILITY_AUTOMATIC
        and replay_attempts < max_attempts
    )


def claim_eligibilities(*, allow_manual: bool) -> tuple[str, ...]:
    if allow_manual:
        return (ELIGIBILITY_AUTOMATIC, ELIGIBILITY_MANUAL)
    return (ELIGIBILITY_AUTOMATIC,)


def historical_sql_case() -> tuple[str, str]:
    """0075 回填使用的 CASE，与 classify_raw_disposition(historical=True) 对齐。"""

    parse_state = """
        CASE
          WHEN processed THEN 'processed'
          WHEN capture_state='protocol_invalid' THEN 'protocol_invalid'
          WHEN capture_state IN ('truncated','unknown_legacy') THEN 'unattempted'
          WHEN content_encoding<>'identity' THEN 'protocol_invalid'
          WHEN http_status < 200 OR http_status >= 300 THEN 'protocol_invalid'
          WHEN error ILIKE '%raw payload integrity mismatch%' THEN 'protocol_invalid'
          WHEN error ILIKE '%OperationalError%'
            OR error ILIKE '%InterfaceError%'
            OR error ILIKE '%Deadlock%'
            OR error ILIKE '%LockNotAvailable%'
            OR error ILIKE '%SerializationError%'
            OR error ILIKE '%CancelledError%'
            OR error ILIKE '%TimeoutError%'
            OR error ILIKE '%connection reset%'
            OR error ILIKE '%connection refused%'
            OR error ILIKE '%too many connections%'
            OR error ILIKE '%server closed the connection%'
            THEN 'transient_failure'
          WHEN error ILIKE '%VendorProtocolError%'
            OR error ILIKE '%VendorApiError%'
            OR error ILIKE '%vendor response parsing failed%'
            OR error ILIKE '%vendor response is not JSON%'
            OR error ILIKE '%vendor response envelope%'
            OR error ILIKE '%content-encoding%'
            OR error ILIKE '%vendor HTTP status%'
            OR error ILIKE '%raw vendor envelope is invalid%'
            OR error ILIKE '%must be an object array%'
            OR error ILIKE 'skipped % invalid % items'
            THEN 'protocol_invalid'
          ELSE 'unattempted'
        END
    """
    eligibility = """
        CASE
          WHEN processed THEN 'never'
          WHEN capture_state IN ('truncated','protocol_invalid','unknown_legacy') THEN 'never'
          WHEN content_encoding<>'identity' THEN 'never'
          WHEN http_status < 200 OR http_status >= 300 THEN 'never'
          WHEN capture_state='complete_too_large' THEN 'manual'
          WHEN error ILIKE '%raw payload integrity mismatch%' THEN 'never'
          WHEN error ILIKE '%VendorApiError%'
            OR error ILIKE '%content-encoding is forbidden%'
            OR error ILIKE '%vendor HTTP status%'
            OR error ILIKE '%vendor response content-encoding%'
            THEN 'never'
          WHEN error ILIKE '%OperationalError%'
            OR error ILIKE '%InterfaceError%'
            OR error ILIKE '%Deadlock%'
            OR error ILIKE '%LockNotAvailable%'
            OR error ILIKE '%SerializationError%'
            OR error ILIKE '%CancelledError%'
            OR error ILIKE '%TimeoutError%'
            OR error ILIKE '%connection reset%'
            OR error ILIKE '%connection refused%'
            OR error ILIKE '%too many connections%'
            OR error ILIKE '%server closed the connection%'
            THEN 'automatic'
          WHEN error ILIKE '%VendorProtocolError%'
            OR error ILIKE '%vendor response parsing failed%'
            OR error ILIKE '%vendor response is not JSON%'
            OR error ILIKE '%vendor response envelope%'
            OR error ILIKE '%raw vendor envelope is invalid%'
            OR error ILIKE '%must be an object array%'
            OR error ILIKE 'skipped % invalid % items'
            THEN 'manual'
          WHEN error IS NULL OR btrim(error)='' THEN
            CASE WHEN capture_state='complete' THEN 'automatic' ELSE 'manual' END
          ELSE 'manual'
        END
    """
    return parse_state, eligibility


def disposition_payload(disposition: RawParseDisposition) -> dict[str, Any]:
    """审计/API 只返回无 PII 的资格事实。"""

    return {
        "parse_state": disposition.parse_state,
        "replay_eligibility": disposition.replay_eligibility,
        "reason": disposition.reason,
        "parser_version": RAW_PARSER_VERSION,
    }


def persist_column_values(
    *,
    capture_state: str | None,
    http_status: object,
    content_encoding: str | None,
) -> dict[str, str]:
    """persist_raw 写入的解析面列；HTTP/编码在落库当时即可判定。"""

    status: int | None
    if isinstance(http_status, bool) or http_status is None:
        status = None
    elif isinstance(http_status, int):
        status = http_status
    elif isinstance(http_status, str) and http_status.strip().lstrip("-").isdigit():
        status = int(http_status)
    else:
        status = None
    disposition = persist_disposition(
        capture_state=capture_state,
        http_status=status,
        content_encoding=content_encoding,
    )
    return {
        "parse_state": disposition.parse_state,
        "replay_eligibility": disposition.replay_eligibility,
    }


def mark_error_column_values(error: str, exc: BaseException | None = None) -> dict[str, str]:
    """解析失败后的列值；调用方用 SQL 保住已是 never 的资格。"""

    disposition = classify_raw_disposition(error=error, exc=exc)
    return {
        "parse_state": disposition.parse_state,
        "replay_eligibility": disposition.replay_eligibility,
    }
