"""spill 与加密流的认证读取；恢复路径一次只物化一个文件。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import BinaryIO

from app.services.crypto import (
    EncryptionContext,
    UnknownKeyVersionError,
)
from app.services.raw_spill_codec import (
    _crypto_has_version,
    _normalize_http_status,
    _require_content_encoding,
    _require_http_status,
    _require_key_version,
    assemble_stream_handle,
    decrypt_spill_header,
    parse_spill_header_frame,
)
from app.services.raw_spill_format import (
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    CAPTURE_UNKNOWN_LEGACY,
    LOGGER,
    REASON_AUTH_FAILED,
    REASON_CORRUPT,
    REASON_KEY_UNAVAILABLE,
    REASON_TRANSIENT_IO,
    SPILL_MAGIC,
    STREAM_MAGIC,
    RawSpillRecord,
    RecoverMemoryProbe,
    SpillMetadataAuthError,
    StreamChunkCrypto,
    _StreamFileHeader,
    artifact_id_from_token,
    normalize_capture_state,
    parse_spill_filename,
)
from app.services.raw_spill_layout import (
    SpillLayout,
)


class SpillReader:
    """读取 .spill / .stream 并认证元数据；只在内存中解密，不回写明文。"""

    def __init__(self, layout: SpillLayout, probe: RecoverMemoryProbe | None) -> None:
        self._layout = layout
        self._probe = probe

    def probe_acquire(self, nbytes: int) -> None:
        if self._probe is not None:
            self._probe.acquire(nbytes)

    def probe_release(self, nbytes: int) -> None:
        if self._probe is not None:
            self._probe.release(nbytes)

    def _probe_payload_read(self, name: str) -> None:
        if self._probe is not None:
            self._probe.note_payload_read(name)

    def inspect_spill_header_auth(self, path: Path, crypto: StreamChunkCrypto) -> str | None:
        """回收路径只认证 header 帧。成功返回 None，否则返回 typed reason。"""

        try:
            with path.open("rb") as handle:
                magic = handle.read(len(SPILL_MAGIC))
                if magic != SPILL_MAGIC:
                    return None
                parsed = parse_spill_header_frame(handle)
                if parsed is None:
                    return REASON_CORRUPT
                meta, ciphertext = parsed
                try:
                    visible = json.loads(meta.decode("utf-8"))
                    hint = _require_key_version(visible["key_version"])
                except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                    return REASON_AUTH_FAILED
                if not _crypto_has_version(crypto, hint):
                    return REASON_KEY_UNAVAILABLE
                decrypt_spill_header(meta, ciphertext, crypto)
                return None
        except UnknownKeyVersionError:
            return REASON_KEY_UNAVAILABLE
        except SpillMetadataAuthError:
            return REASON_AUTH_FAILED
        except OSError:
            return REASON_TRANSIENT_IO
        except (TypeError, ValueError, UnicodeError):
            return REASON_AUTH_FAILED

    def read(
        self,
        path: Path,
        *,
        expected_source: str | None = None,
        crypto: StreamChunkCrypto | None = None,
    ) -> RawSpillRecord | None:
        try:
            with path.open("rb") as handle:
                magic = handle.read(len(SPILL_MAGIC))
                if magic == SPILL_MAGIC:
                    return self._read_authenticated_spill(
                        handle, path, expected_source=expected_source, crypto=crypto
                    )
                handle.seek(0)
                return self._read_legacy_spill(handle, path, expected_source=expected_source)
        except SpillMetadataAuthError:
            raise
        except UnknownKeyVersionError:
            raise
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _read_authenticated_spill(
        self,
        handle: BinaryIO,
        path: Path,
        *,
        expected_source: str | None,
        crypto: StreamChunkCrypto | None,
    ) -> RawSpillRecord | None:
        parsed_frame = parse_spill_header_frame(handle)
        if parsed_frame is None:
            return None
        meta, ciphertext = parsed_frame
        try:
            visible = json.loads(meta.decode("utf-8"))
            source = str(visible["source"])
            payload_sha256 = str(visible["payload_sha256"])
            payload_enc_sha256 = str(visible["payload_enc_sha256"])
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            if crypto is not None:
                raise SpillMetadataAuthError("spill header is unreadable") from exc
            return None
        if expected_source is not None and source != expected_source:
            return None
        if not self._layout.spill_path_matches_identity(path, source, payload_sha256):
            if crypto is not None:
                raise SpillMetadataAuthError("spill filename identity mismatch")
            return None
        authenticated: dict[str, object] | None = None
        if crypto is not None:
            authenticated = decrypt_spill_header(meta, ciphertext, crypto)
            source = str(authenticated["source"])
            payload_sha256 = str(authenticated["payload_sha256"])
            payload_enc_sha256 = str(authenticated["payload_enc_sha256"])
            if not self._layout.spill_path_matches_identity(path, source, payload_sha256):
                raise SpillMetadataAuthError("spill filename identity mismatch")
            if expected_source is not None and source != expected_source:
                return None
        self._probe_payload_read(path.name)
        payload_enc = handle.read()
        if not payload_enc:
            return None
        if hashlib.sha256(payload_enc).hexdigest() != payload_enc_sha256:
            if crypto is not None:
                raise SpillMetadataAuthError("spill payload digest mismatch")
            return None
        fields = authenticated or visible
        parsed_name = parse_spill_filename(path.name)
        artifact_id = artifact_id_from_token(parsed_name[1]) if parsed_name else ""
        return RawSpillRecord(
            source=source,
            payload_sha256=payload_sha256,
            key_version=_require_key_version(fields["key_version"]),
            http_status=_require_http_status(fields["http_status"]),
            content_encoding=_require_content_encoding(fields["content_encoding"]),
            payload_enc=payload_enc,
            path=path,
            capture_state=normalize_capture_state(str(fields["capture_state"])),
            plaintext_bytes=len(payload_enc),
            metadata_authenticated=authenticated is not None,
            format_legacy=False,
            artifact_id=artifact_id,
        )

    def _read_legacy_spill(
        self,
        handle: BinaryIO,
        path: Path,
        *,
        expected_source: str | None,
    ) -> RawSpillRecord | None:
        header_bytes = handle.readline()
        header = json.loads(header_bytes.decode("utf-8"))
        source = str(header["source"])
        if expected_source is not None and source != expected_source:
            return None
        payload_sha256 = str(header["payload_sha256"])
        if not self._layout.spill_path_matches_identity(path, source, payload_sha256):
            return None
        self._probe_payload_read(path.name)
        payload_enc = handle.read()
        if not payload_enc:
            return None
        try:
            key_version = _require_key_version(header.get("key_version", 1))
        except ValueError:
            key_version = 1
        try:
            http_status = _require_http_status(header.get("http_status", 200))
        except ValueError:
            http_status = 200
        try:
            content_encoding = _require_content_encoding(header.get("content_encoding", "identity"))
        except ValueError:
            content_encoding = "identity"
        return RawSpillRecord(
            source=source,
            payload_sha256=payload_sha256,
            key_version=key_version,
            http_status=http_status,
            content_encoding=content_encoding,
            payload_enc=payload_enc,
            path=path,
            capture_state=CAPTURE_UNKNOWN_LEGACY,
            plaintext_bytes=len(payload_enc),
            metadata_authenticated=False,
            format_legacy=True,
        )

    def parse_stream_header_handle(self, handle: BinaryIO, path: Path) -> _StreamFileHeader | None:
        try:
            magic = handle.read(len(STREAM_MAGIC))
            if magic != STREAM_MAGIC:
                return None
            header_bytes = handle.readline()
            if not header_bytes.endswith(b"\n"):
                return None
            header = json.loads(header_bytes.decode("utf-8"))
            source = str(header["source"])
            stream_id = str(header["stream_id"])
            key_version = int(header["key_version"])
            expected_tmp = self._layout.stream_tmp(source, stream_id)
            expected_final = self._layout.stream_path(source, stream_id)
            legacy_final = expected_final.with_suffix(".stream.stream")
            if path not in {expected_tmp, expected_final, legacy_final}:
                parsed = parse_spill_filename(path.name)
                if parsed is None or parsed[0] != source or not parsed[2].startswith(".cq"):
                    return None
            return _StreamFileHeader(source, stream_id, key_version, path)
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def read_stream(
        self,
        path: Path,
        crypto: StreamChunkCrypto,
        *,
        expected_source: str | None = None,
    ) -> RawSpillRecord | None:
        try:
            handle = path.open("rb")
        except OSError:
            return None
        try:
            header = self.parse_stream_header_handle(handle, path)
            if header is None:
                return None
            if expected_source is not None and header.source != expected_source:
                return None
            self._probe_payload_read(path.name)
            assembled = assemble_stream_handle(handle, header, crypto)
        except UnknownKeyVersionError:
            raise
        except OSError:
            return None
        except Exception as exc:
            LOGGER.warning(
                "raw spill stream recover skipped",
                extra={"error_type": type(exc).__name__},
            )
            return None
        finally:
            handle.close()
        if assembled.empty:
            return None
        try:
            payload_sha256 = assembled.digest
            encrypted = crypto.encrypt_bound_bytes(
                assembled.plaintext,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_vendor_log",
                    column="payload_enc",
                    object_id=f"{header.source}:{payload_sha256}",
                ),
            )
        except (ValueError, TypeError):
            return None
        quarantined = False
        metadata_authenticated = True
        if assembled.terminal is not None and not assembled.incomplete:
            capture_state = normalize_capture_state(str(assembled.terminal["capture_state"]))
            http_status = _normalize_http_status(assembled.terminal["http_status"])
            content_encoding = str(assembled.terminal["content_encoding"])
            if assembled.legacy_terminal:
                capture_state = CAPTURE_UNKNOWN_LEGACY
                metadata_authenticated = False
        else:
            announce = assembled.announce or {}
            announced_state = (
                str(announce.get("capture_state")) if announce.get("capture_state") else ""
            )
            if announced_state == CAPTURE_PROTOCOL_INVALID:
                capture_state = CAPTURE_PROTOCOL_INVALID
            else:
                capture_state = CAPTURE_TRUNCATED
            http_status = _normalize_http_status(announce.get("http_status")) if announce else 200
            content_encoding = (
                str(announce.get("content_encoding") or "identity") if announce else "identity"
            )
            self._layout.write_quarantine(header.source, header.stream_id)
            quarantined = True
        return RawSpillRecord(
            source=header.source,
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=http_status,
            content_encoding=content_encoding,
            payload_enc=encrypted.payload,
            path=path,
            capture_state=capture_state,
            quarantined=quarantined,
            stream_id=header.stream_id,
            plaintext_bytes=len(assembled.plaintext),
            metadata_authenticated=metadata_authenticated,
            format_legacy=assembled.legacy_terminal,
        )
