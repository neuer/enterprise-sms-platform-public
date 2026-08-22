"""pre-0072 raw 完整性分类：只使用密文长度与错误类型，不解密正文。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_TRUNCATED,
    CAPTURE_UNKNOWN_LEGACY,
)

# 与 vendor/zhihui.py 自动解析 / 恢复捕获上限对齐；分类不得解密正文。
AUTO_PARSE_LIMIT_BYTES = 4 * 1024 * 1024
RECOVERY_CAPTURE_LIMIT_BYTES = 64 * 1024 * 1024
BOUND_ENVELOPE_MAGIC = b"SME2"
BOUND_ENVELOPE_OVERHEAD_BYTES = 32  # SME2 + nonce + GCM tag
LEGACY_ENVELOPE_OVERHEAD_BYTES = 28  # nonce + GCM tag
MIN_VALID_CIPHERTEXT_BYTES = LEGACY_ENVELOPE_OVERHEAD_BYTES

# #413 合并到 #415 合并的中间部署窗口；只用于盘点，不作为分类主证据。
INTERMEDIATE_WINDOW_START = datetime(2026, 8, 22, 6, 47, 6, tzinfo=UTC)
INTERMEDIATE_WINDOW_END = datetime(2026, 8, 22, 10, 21, 50, tzinfo=UTC)

ERROR_TRUNCATED_MARKERS = (
    "truncated vendor response",
    "beyond recovery limit",
    "exceeds recovery capture limit",
    "exceeded raw spill quota",
    "exceeds hard limit",
    "body exceeds hard limit",
)
ERROR_TOO_LARGE_MARKERS = (
    "oversized payload persisted",
    "exceeds automatic processing limit",
    "too large to parse",
)

AUTO_REPLAY_STATES = frozenset({CAPTURE_COMPLETE})
OPS_REPLAY_STATES = frozenset({CAPTURE_COMPLETE, CAPTURE_COMPLETE_TOO_LARGE})
VALID_LEGACY_CAPTURE_STATES = frozenset(
    {
        CAPTURE_COMPLETE,
        CAPTURE_COMPLETE_TOO_LARGE,
        CAPTURE_TRUNCATED,
        CAPTURE_UNKNOWN_LEGACY,
    }
)

# 盘点输出禁止出现的键；密文、哈希、号码与密钥材料一律不得出报告。
FORBIDDEN_INVENTORY_KEYS = frozenset(
    {
        "aes_key",
        "content",
        "hmac_key",
        "key_material",
        "mobile",
        "payload",
        "payload_enc",
        "payload_sha256",
        "phone",
        "phone_enc",
        "phone_hmac",
        "phone_mask",
        "raw_body",
        "secret",
        "secret_key",
    }
)
PHONE_PATTERN = r"1\d{10}"


@dataclass(frozen=True, slots=True)
class RawLegacyEvidence:
    """可供分类的无明文证据。payload_prefix 只取密文头，不得含正文。"""

    enc_len: int
    payload_prefix: bytes = b""
    error: str | None = None
    processed: bool = False


@dataclass(frozen=True, slots=True)
class ReplayEligibility:
    auto: bool
    ops: bool
    reason: str


def envelope_overhead_bytes(payload_prefix: bytes) -> int:
    """按信封魔数估计密文开销；无法识别时按更短的 legacy 信封计算。"""

    if payload_prefix.startswith(BOUND_ENVELOPE_MAGIC):
        return BOUND_ENVELOPE_OVERHEAD_BYTES
    return LEGACY_ENVELOPE_OVERHEAD_BYTES


def estimate_plaintext_len(enc_len: int, payload_prefix: bytes = b"") -> int | None:
    """由密文长度估计明文长度；信封过短则无法估计。"""

    if enc_len < 1:
        return None
    overhead = envelope_overhead_bytes(payload_prefix)
    if enc_len < overhead:
        return None
    return enc_len - overhead


def classify_error(error: str | None) -> str | None:
    """把历史 error 摘要映射到截断或超限完整；无法识别则返回 None。"""

    text = (error or "").casefold()
    if not text:
        return None
    if any(marker in text for marker in ERROR_TRUNCATED_MARKERS):
        return CAPTURE_TRUNCATED
    if any(marker in text for marker in ERROR_TOO_LARGE_MARKERS):
        return CAPTURE_COMPLETE_TOO_LARGE
    return None


def classify_historical_raw(evidence: RawLegacyEvidence) -> str:
    """把 pre-0072 存量 raw 分类到 capture_state；冲突或证据不足则 unknown_legacy。"""

    error_state = classify_error(evidence.error)
    estimated = estimate_plaintext_len(evidence.enc_len, evidence.payload_prefix)

    if error_state == CAPTURE_TRUNCATED:
        return CAPTURE_TRUNCATED
    if error_state == CAPTURE_COMPLETE_TOO_LARGE:
        if estimated is not None and estimated > RECOVERY_CAPTURE_LIMIT_BYTES:
            return CAPTURE_UNKNOWN_LEGACY
        return CAPTURE_COMPLETE_TOO_LARGE
    if estimated is None:
        return CAPTURE_UNKNOWN_LEGACY
    if estimated > RECOVERY_CAPTURE_LIMIT_BYTES:
        return CAPTURE_TRUNCATED
    if estimated == RECOVERY_CAPTURE_LIMIT_BYTES:
        return CAPTURE_UNKNOWN_LEGACY
    if estimated > AUTO_PARSE_LIMIT_BYTES:
        return CAPTURE_COMPLETE_TOO_LARGE
    if estimated == AUTO_PARSE_LIMIT_BYTES and not evidence.processed:
        return CAPTURE_UNKNOWN_LEGACY
    return CAPTURE_COMPLETE


def replay_forbidden_message(capture_state: str) -> str | None:
    """返回禁止普通重放的中文原因；complete / complete_too_large 返回 None。"""

    if capture_state == CAPTURE_TRUNCATED:
        return "截断 raw 不得当作正常可重放"
    if capture_state == CAPTURE_UNKNOWN_LEGACY:
        return "未分类历史 raw 不得当作正常可重放，需人工盘点后再提升"
    if capture_state not in OPS_REPLAY_STATES:
        return "该完整性状态不得当作正常可重放"
    return None


def replay_eligibility(
    capture_state: str | None,
    *,
    processed: bool,
    replay_attempts: int = 0,
    max_attempts: int = 10,
) -> ReplayEligibility:
    """计算自动/运维重放资格。缺省 capture_state 视为旧 schema 的 complete。"""

    state = capture_state or CAPTURE_COMPLETE
    if processed:
        return ReplayEligibility(False, False, "already_processed")
    forbidden = replay_forbidden_message(state)
    if forbidden is not None:
        return ReplayEligibility(False, False, state)
    ops = state in OPS_REPLAY_STATES
    auto = (
        state in AUTO_REPLAY_STATES
        and replay_attempts < max_attempts
    )
    if auto:
        return ReplayEligibility(True, True, "complete")
    if ops:
        return ReplayEligibility(False, True, "complete_too_large")
    return ReplayEligibility(False, False, state)


def needs_complete_reclassify(current_state: str, evidence: RawLegacyEvidence) -> bool:
    """已回填为 complete 的行是否仍带有超限/截断/不可判定证据。"""

    if current_state != CAPTURE_COMPLETE:
        return False
    return classify_historical_raw(evidence) != CAPTURE_COMPLETE


def in_intermediate_window(fetched_at: datetime | None) -> bool:
    if fetched_at is None:
        return False
    moment = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return INTERMEDIATE_WINDOW_START <= moment <= INTERMEDIATE_WINDOW_END


def size_bucket(estimated: int | None) -> str:
    if estimated is None:
        return "invalid_envelope"
    if estimated > RECOVERY_CAPTURE_LIMIT_BYTES:
        return "gt_64mib"
    if estimated == RECOVERY_CAPTURE_LIMIT_BYTES:
        return "eq_64mib"
    if estimated > AUTO_PARSE_LIMIT_BYTES:
        return "gt_4mib_le_64mib"
    if estimated == AUTO_PARSE_LIMIT_BYTES:
        return "eq_4mib"
    return "lt_4mib"


def error_class(error: str | None) -> str:
    classified = classify_error(error)
    if classified == CAPTURE_TRUNCATED:
        return "truncated"
    if classified == CAPTURE_COMPLETE_TOO_LARGE:
        return "complete_too_large"
    if error:
        return "other"
    return "empty"


@dataclass(frozen=True, slots=True)
class RawInventoryInput:
    """盘点输入。只允许密文长度与 4 字节头，禁止正文、号码和密钥。"""

    source: str
    processed: bool
    enc_len: int
    payload_prefix: bytes = b""
    error: str | None = None
    fetched_at: datetime | None = None
    capture_state: str | None = None
    replay_attempts: int = 0
    raw_id: int | None = None


def _count(bucket: dict[str, int], key: str) -> None:
    bucket[key] = bucket.get(key, 0) + 1


def build_inventory(rows: Sequence[RawInventoryInput]) -> dict[str, Any]:
    """汇总数量、大小桶、状态和窗口；输出不得含解密正文、号码或密钥。"""

    by_state: dict[str, int] = {}
    by_processed: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_size: dict[str, int] = {}
    by_error: dict[str, int] = {}
    by_replay: dict[str, int] = {"auto": 0, "ops_only": 0, "forbidden": 0, "processed": 0}
    window = {"in_413_415": 0, "outside": 0, "unknown_fetched_at": 0}
    fetched_bounds: dict[str, dict[str, str | None]] = {}
    review_ids: list[int] = []

    for row in rows:
        evidence = RawLegacyEvidence(
            enc_len=row.enc_len,
            payload_prefix=row.payload_prefix[:4],
            error=row.error,
            processed=row.processed,
        )
        state = row.capture_state or classify_historical_raw(evidence)
        estimated = estimate_plaintext_len(row.enc_len, row.payload_prefix[:4])
        eligibility = replay_eligibility(
            state,
            processed=row.processed,
            replay_attempts=row.replay_attempts,
        )
        _count(by_state, state)
        _count(by_processed, "processed" if row.processed else "unprocessed")
        _count(by_source, row.source)
        _count(by_size, size_bucket(estimated))
        _count(by_error, error_class(row.error))
        if row.processed:
            by_replay["processed"] += 1
        elif eligibility.auto:
            by_replay["auto"] += 1
        elif eligibility.ops:
            by_replay["ops_only"] += 1
        else:
            by_replay["forbidden"] += 1
        if row.fetched_at is None:
            window["unknown_fetched_at"] += 1
        elif in_intermediate_window(row.fetched_at):
            window["in_413_415"] += 1
        else:
            window["outside"] += 1
        bounds = fetched_bounds.setdefault(state, {"min": None, "max": None})
        if row.fetched_at is not None:
            stamp = row.fetched_at.astimezone(UTC).isoformat()
            if bounds["min"] is None or stamp < str(bounds["min"]):
                bounds["min"] = stamp
            if bounds["max"] is None or stamp > str(bounds["max"]):
                bounds["max"] = stamp
        if (
            state in {CAPTURE_UNKNOWN_LEGACY, CAPTURE_TRUNCATED}
            and not row.processed
            and row.raw_id is not None
            and len(review_ids) < 200
        ):
            review_ids.append(int(row.raw_id))

    return {
        "schema": "raw_capture_legacy_inventory.v1",
        "row_count": len(rows),
        "by_capture_state": by_state,
        "by_processed": by_processed,
        "by_source": by_source,
        "by_size_bucket": by_size,
        "by_error_class": by_error,
        "by_replay_eligibility": by_replay,
        "windows": {
            "intermediate_413_415": {
                "start": INTERMEDIATE_WINDOW_START.isoformat(),
                "end": INTERMEDIATE_WINDOW_END.isoformat(),
                **window,
            }
        },
        "fetched_at_bounds_by_state": fetched_bounds,
        "review_raw_ids": review_ids,
        "disposition": {
            "complete": "auto_replay_if_unprocessed",
            "complete_too_large": "ops_replay_only",
            "truncated": "no_normal_auto_or_ops_replay",
            "unknown_legacy": "human_review_then_promote",
        },
    }


def _walk_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


def inventory_leak_reasons(document: Mapping[str, Any]) -> list[str]:
    """检查盘点结果是否泄漏正文、号码或密钥材料。"""

    reasons: list[str] = []
    for key in _walk_keys(document):
        if key in FORBIDDEN_INVENTORY_KEYS:
            reasons.append(f"forbidden_key:{key}")
    serialized = json.dumps(document, ensure_ascii=False, default=str)
    lowered = serialized.casefold()
    if re.search(PHONE_PATTERN, serialized):
        reasons.append("phone_number")
    for marker in ("-----begin", "secretkey", "aes-256-gcm"):
        if marker in lowered:
            reasons.append(f"key_material:{marker}")
    return reasons
