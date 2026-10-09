"""spill 认证 header、控制帧与加密流帧的无状态编解码；不触碰目录状态。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag

from app.services.crypto import (
    EncryptionContext,
    UnknownKeyVersionError,
)
from app.services.raw_spill_format import (
    MAX_CLASSIFY_CONTROL_BYTES,
    SHA256_PATTERN,
    SOURCE_PATTERN,
    STREAM_CONTROL_SENTINEL,
    STREAM_KIND_ANNOUNCE,
    STREAM_KIND_SPILL,
    STREAM_KIND_TERMINAL,
    STREAM_META_HEADER,
    STREAM_RECORD_HEADER,
    SpillMetadataAuthError,
    StreamChunkCrypto,
    _AssembledStream,
    _StreamFileHeader,
    normalize_capture_state,
)


def _normalize_http_status(value: object) -> int:
    status = int(value) if isinstance(value, (int, str)) else 200
    return status if 100 <= status <= 599 else 200


def _control_object_id(
    *,
    source: str,
    stream_id: str,
    seq: int,
    kind: str,
    http_status: int,
    content_encoding: str,
    capture_state: str,
) -> str:
    return f"{source}:{stream_id}:{seq:08d}:{kind}:{http_status}:{content_encoding}:{capture_state}"


def _control_context(
    *,
    source: str,
    stream_id: str,
    seq: int,
    kind: str,
    http_status: int,
    content_encoding: str,
    capture_state: str,
) -> EncryptionContext:
    return EncryptionContext(
        domain="vendor-raw",
        table="raw_spill",
        column=kind,
        object_id=_control_object_id(
            source=source,
            stream_id=stream_id,
            seq=seq,
            kind=kind,
            http_status=http_status,
            content_encoding=content_encoding,
            capture_state=capture_state,
        ),
    )


def _require_http_status(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid http_status")
    status = int(value)
    if not 100 <= status <= 599:
        raise ValueError("invalid http_status")
    return status


def _require_key_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid key_version")
    version = int(value)
    if not 1 <= version <= 32767:
        raise ValueError("invalid key_version")
    return version


def _require_content_encoding(value: object) -> str:
    encoding = str(value or "")
    if not encoding or len(encoding) > 64 or "/" in encoding or "\\" in encoding:
        raise ValueError("invalid content_encoding")
    return encoding


def canonical_spill_header(
    *,
    source: str,
    payload_sha256: str,
    payload_enc_sha256: str,
    key_version: int,
    http_status: int,
    content_encoding: str,
    capture_state: str,
) -> bytes:
    """规范化 secondary spill 认证 header；任一字段变化都会改变正文 AAD。"""

    if SOURCE_PATTERN.fullmatch(source) is None:
        raise ValueError("invalid raw spill source")
    if SHA256_PATTERN.fullmatch(payload_sha256) is None:
        raise ValueError("invalid raw spill digest")
    if SHA256_PATTERN.fullmatch(payload_enc_sha256) is None:
        raise ValueError("invalid raw spill ciphertext digest")
    document = {
        "capture_state": normalize_capture_state(capture_state),
        "content_encoding": _require_content_encoding(content_encoding),
        "http_status": _require_http_status(http_status),
        "key_version": _require_key_version(key_version),
        "kind": STREAM_KIND_SPILL,
        "payload_enc_sha256": payload_enc_sha256,
        "payload_sha256": payload_sha256,
        "source": source,
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _spill_header_context(meta: dict[str, object]) -> EncryptionContext:
    source = str(meta["source"])
    payload_sha256 = str(meta["payload_sha256"])
    payload_enc_sha256 = str(meta["payload_enc_sha256"])
    http_status = _require_http_status(meta["http_status"])
    content_encoding = _require_content_encoding(meta["content_encoding"])
    capture_state = normalize_capture_state(str(meta["capture_state"]))
    key_version = _require_key_version(meta["key_version"])
    object_id = (
        f"{source}:{payload_sha256}:{payload_enc_sha256}:"
        f"{http_status}:{content_encoding}:{capture_state}:{key_version}"
    )
    return EncryptionContext(
        domain="vendor-raw",
        table="raw_spill",
        column=STREAM_KIND_SPILL,
        object_id=object_id,
    )


def _crypto_key_versions(crypto: StreamChunkCrypto) -> tuple[int, ...]:
    raw_versions = getattr(crypto, "key_versions", None)
    if not raw_versions:
        return ()
    versions: list[int] = []
    for value in raw_versions:
        version = int(value)
        if version not in versions:
            versions.append(version)
    return tuple(versions)


def _crypto_has_version(crypto: StreamChunkCrypto, version: int) -> bool:
    versions = _crypto_key_versions(crypto)
    return not versions or version in versions


def candidate_key_versions(crypto: StreamChunkCrypto, hint: int | None = None) -> tuple[int, ...]:
    """认证尝试顺序：提示版本与 active 优先，但必须覆盖整个 keyring。"""

    ordered: list[int] = []
    for value in (hint, getattr(crypto, "active_version", None)):
        if isinstance(value, int) and not isinstance(value, bool) and value not in ordered:
            ordered.append(value)
    for version in _crypto_key_versions(crypto):
        if version not in ordered:
            ordered.append(version)
    if not ordered:
        ordered = [1]
    return tuple(ordered)


def parse_spill_header_frame(handle: BinaryIO) -> tuple[bytes, bytes] | None:
    raw_len = handle.read(STREAM_RECORD_HEADER.size)
    if len(raw_len) < STREAM_RECORD_HEADER.size:
        return None
    (frame_len,) = STREAM_RECORD_HEADER.unpack(raw_len)
    if frame_len < STREAM_META_HEADER.size or frame_len > MAX_CLASSIFY_CONTROL_BYTES:
        return None
    frame = handle.read(frame_len)
    if len(frame) != frame_len:
        return None
    (meta_len,) = STREAM_META_HEADER.unpack_from(frame)
    if STREAM_META_HEADER.size + meta_len > len(frame):
        return None
    meta = frame[STREAM_META_HEADER.size : STREAM_META_HEADER.size + meta_len]
    ciphertext = frame[STREAM_META_HEADER.size + meta_len :]
    return meta, ciphertext


def decrypt_spill_header(
    meta: bytes, ciphertext: bytes, crypto: StreamChunkCrypto
) -> dict[str, object]:
    try:
        parsed = json.loads(meta.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise SpillMetadataAuthError("spill header is not an object")
        canonical = canonical_spill_header(
            source=str(parsed["source"]),
            payload_sha256=str(parsed["payload_sha256"]),
            payload_enc_sha256=str(parsed["payload_enc_sha256"]),
            key_version=_require_key_version(parsed["key_version"]),
            http_status=_require_http_status(parsed["http_status"]),
            content_encoding=_require_content_encoding(parsed["content_encoding"]),
            capture_state=str(parsed["capture_state"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpillMetadataAuthError("spill header is not canonical") from exc
    if canonical != meta:
        raise SpillMetadataAuthError("spill header is not canonical")
    context = _spill_header_context(parsed)
    hint = _require_key_version(parsed["key_version"])
    if not _crypto_has_version(crypto, hint):
        raise UnknownKeyVersionError(hint)
    last_error: Exception | None = None
    for version in candidate_key_versions(crypto, hint):
        try:
            plaintext = crypto.decrypt_bound_bytes(ciphertext, version, context)
        except UnknownKeyVersionError:
            raise
        except (ValueError, TypeError, InvalidTag) as exc:
            last_error = exc
            continue
        if plaintext != meta:
            raise SpillMetadataAuthError("spill header plaintext mismatch")
        authenticated = json.loads(plaintext.decode("utf-8"))
        if not isinstance(authenticated, dict):
            raise SpillMetadataAuthError("spill header is not an object")
        if _require_key_version(authenticated["key_version"]) != hint:
            raise SpillMetadataAuthError("spill header key_version mismatch")
        return authenticated
    raise SpillMetadataAuthError("spill header authentication failed") from last_error


def read_control_frame(
    body: bytes,
    offset: int,
    crypto: StreamChunkCrypto,
    *,
    source: str,
    stream_id: str,
    key_version: int,
    seq: int,
) -> tuple[dict[str, object] | None, int, bool]:
    """解析长度化 AES-GCM 控制帧；失败只报告 incomplete，不得抛穿。"""

    if offset + STREAM_RECORD_HEADER.size > len(body):
        return None, offset, False
    (frame_len,) = STREAM_RECORD_HEADER.unpack_from(body, offset)
    offset += STREAM_RECORD_HEADER.size
    if frame_len < STREAM_META_HEADER.size or offset + frame_len > len(body):
        return None, offset, False
    frame = body[offset : offset + frame_len]
    offset += frame_len
    (meta_len,) = STREAM_META_HEADER.unpack_from(frame)
    if STREAM_META_HEADER.size + meta_len > len(frame):
        return None, offset, False
    meta = frame[STREAM_META_HEADER.size : STREAM_META_HEADER.size + meta_len]
    ciphertext = frame[STREAM_META_HEADER.size + meta_len :]
    try:
        header = json.loads(meta.decode("utf-8"))
        kind = str(header["kind"])
        http_status = _normalize_http_status(header.get("http_status"))
        content_encoding = str(header.get("content_encoding") or "identity")
        capture_state = normalize_capture_state(str(header.get("capture_state")))
        plaintext = crypto.decrypt_bound_bytes(
            ciphertext,
            key_version,
            _control_context(
                source=source,
                stream_id=stream_id,
                seq=seq,
                kind=kind,
                http_status=http_status,
                content_encoding=content_encoding,
                capture_state=capture_state,
            ),
        )
        if plaintext != meta:
            return None, offset, False
        return (
            {
                "kind": kind,
                "http_status": http_status,
                "content_encoding": content_encoding,
                "capture_state": capture_state,
            },
            offset,
            True,
        )
    except UnknownKeyVersionError:
        raise
    except InvalidTag:
        return None, offset, False
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None, offset, False


def parse_legacy_footer(rest: bytes) -> dict[str, object] | None:
    """兼容 #415 明文 footer；不完整 JSON 一律视为 terminal 失败。"""

    if not rest or b"\n" not in rest:
        return None
    try:
        line = rest.split(b"\n", maxsplit=1)[0]
        footer = json.loads(line.decode("utf-8"))
        return {
            "kind": STREAM_KIND_TERMINAL,
            "http_status": _normalize_http_status(footer.get("http_status", 200)),
            "content_encoding": str(footer.get("content_encoding") or "identity"),
            "capture_state": normalize_capture_state(str(footer.get("capture_state"))),
        }
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def read_control_handle(
    handle: BinaryIO,
    crypto: StreamChunkCrypto,
    header: _StreamFileHeader,
    *,
    seq: int,
) -> tuple[dict[str, object] | None, bool]:
    raw_len = handle.read(STREAM_RECORD_HEADER.size)
    if len(raw_len) < STREAM_RECORD_HEADER.size:
        return None, False
    (frame_len,) = STREAM_RECORD_HEADER.unpack(raw_len)
    if frame_len < STREAM_META_HEADER.size:
        return None, False
    frame = handle.read(frame_len)
    if len(frame) != frame_len:
        return None, False
    fields, _offset, ok = read_control_frame(
        STREAM_RECORD_HEADER.pack(frame_len) + frame,
        0,
        crypto,
        source=header.source,
        stream_id=header.stream_id,
        key_version=header.key_version,
        seq=seq,
    )
    return fields, ok


def assemble_stream_handle(
    handle: BinaryIO,
    header: _StreamFileHeader,
    crypto: StreamChunkCrypto,
) -> _AssembledStream:
    """流式解密：同一时刻只持有当前帧 + 累计明文，不保留 raw 全文与 chunks 列表。"""

    plaintext = bytearray()
    hasher = hashlib.sha256()
    announce: dict[str, object] | None = None
    terminal: dict[str, object] | None = None
    incomplete = False
    legacy_terminal = False
    seq = 0
    while True:
        raw_len = handle.read(STREAM_RECORD_HEADER.size)
        if not raw_len:
            break
        if len(raw_len) < STREAM_RECORD_HEADER.size:
            incomplete = True
            break
        (length,) = STREAM_RECORD_HEADER.unpack(raw_len)
        if length == STREAM_CONTROL_SENTINEL:
            fields, ok = read_control_handle(handle, crypto, header, seq=0)
            if not ok or fields is None or fields.get("kind") != STREAM_KIND_ANNOUNCE:
                incomplete = True
                break
            announce = fields
            continue
        if length == 0:
            marked = handle.tell()
            fields, ok = read_control_handle(handle, crypto, header, seq=seq)
            if ok and fields is not None and fields.get("kind") == STREAM_KIND_TERMINAL:
                terminal = fields
            else:
                handle.seek(marked)
                line = handle.readline()
                footer = line if line.endswith(b"\n") else line + b"\n"
                legacy = parse_legacy_footer(footer)
                if legacy is None:
                    incomplete = True
                else:
                    terminal = legacy
                    legacy_terminal = True
            break
        ciphertext = handle.read(length)
        if len(ciphertext) != length:
            incomplete = True
            break
        try:
            chunk = crypto.decrypt_bound_bytes(
                ciphertext,
                header.key_version,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_spill",
                    column="chunk",
                    object_id=f"{header.source}:{header.stream_id}:{seq:08d}",
                ),
            )
        except UnknownKeyVersionError:
            raise
        except (InvalidTag, ValueError, TypeError):
            incomplete = True
            break
        hasher.update(chunk)
        plaintext.extend(chunk)
        seq += 1
    assembled = bytes(plaintext)
    plaintext.clear()
    return _AssembledStream(
        assembled, hasher.hexdigest(), announce, terminal, incomplete, legacy_terminal
    )


def safe_src_name(name: str) -> str | None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    if name.startswith("."):
        return None
    return name


def file_size_and_sha256(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()
