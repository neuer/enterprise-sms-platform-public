"""#438 T5-01：内部帧合同。网络 chunk 不得映射为持久化 AES-GCM 帧。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import random
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.crypto import (
    BOUND_ENVELOPE_MAGIC,
    NONCE_SIZE,
    TAG_SIZE,
    CryptoService,
    EncryptionContext,
)
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_FRAME_OVERHEAD_BYTES,
    CAPTURE_TRUNCATED,
    CONTROL_FRAME_COUNT,
    DATA_FRAME_OVERHEAD_BYTES,
    INTERNAL_FRAME_SIZE,
    RECOVERY_CAPTURE_BYTES,
    STREAM_RECORD_HEADER,
    RawSpillStore,
    SpillQuotaExceeded,
    capture_reservation_bytes,
    max_internal_frames,
)
from app.vendor.zhihui import (
    RawPulledPayload,
    VendorResponseTooLarge,
    VendorTotalTimeout,
    ZhihuiClient,
    decode_pulled_payload,
)

FOUR_MIB = 4 * 1024 * 1024
CHUNK_KINDS = ("1", "7", "16", "1024", "random")


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class CountingCrypto:
    def __init__(self, inner: CryptoService) -> None:
        self._inner = inner
        self.encrypt_calls = 0
        self.data_frame_sizes: list[int] = []

    def encrypt_bound_bytes(self, plaintext: bytes, context: EncryptionContext) -> Any:
        self.encrypt_calls += 1
        if context.column == "chunk":
            self.data_frame_sizes.append(len(plaintext))
        return self._inner.encrypt_bound_bytes(plaintext, context)

    def decrypt_bound_bytes(self, *args: Any, **kwargs: Any) -> bytes:
        return self._inner.decrypt_bound_bytes(*args, **kwargs)


class FragmentStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes, kind: str, *, seed: int = 438) -> None:
        self.payload = payload
        self.kind = kind
        self.seed = seed
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in iter_chunks(self.payload, self.kind, seed=self.seed):
            self.yielded += 1
            yield chunk


def iter_chunks(payload: bytes, kind: str, *, seed: int = 438) -> Iterator[bytes]:
    if kind == "random":
        rng = random.Random(seed)
        offset = 0
        while offset < len(payload):
            take = rng.randint(1, 2048)
            yield payload[offset : offset + take]
            offset += take
        return
    step = int(kind)
    for offset in range(0, len(payload), step):
        yield payload[offset : offset + step]


def expected_frames(nbytes: int) -> int:
    return max_internal_frames(nbytes)


def report_json(size: int) -> bytes:
    prefix = b'{"code":0,"msg":null,"data":"'
    suffix = b'"}'
    pad = size - len(prefix) - len(suffix)
    if pad < 0:
        raise ValueError("report json size too small")
    return prefix + b"a" * pad + suffix


def make_client(
    handler: Any,
    *,
    body_limit: int = FOUR_MIB,
    capture_limit: int = RECOVERY_CAPTURE_BYTES,
    total_timeout_s: float = 30,
) -> ZhihuiClient:
    return ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://vendor.test",
        ),
        max_response_body_bytes=body_limit,
        max_response_capture_bytes=capture_limit,
        total_timeout_s=total_timeout_s,
    )


def feed_fragmented(stream: Any, payload: bytes, kind: str, *, seed: int = 438) -> bool:
    return all(stream.feed(chunk) for chunk in iter_chunks(payload, kind, seed=seed))


def test_data_frame_overhead_matches_sme2_envelope() -> None:
    service = crypto()
    encrypted = service.encrypt_bound_bytes(
        b"x",
        EncryptionContext(
            domain="vendor-raw",
            table="raw_spill",
            column="chunk",
            object_id="probe",
        ),
    )
    assert len(encrypted.payload) == 1 + len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE
    assert STREAM_RECORD_HEADER.size + (
        len(encrypted.payload) - 1
    ) == DATA_FRAME_OVERHEAD_BYTES


def test_reservation_formula_is_internal_frame_contract() -> None:
    source = inspect.getsource(capture_reservation_bytes)
    assert "INTERNAL_FRAME_SIZE" in source
    assert "DATA_FRAME_OVERHEAD_BYTES" in source
    assert "CONTROL_FRAME" in source
    assert "DIRECTORY_METADATA_BYTES" in source
    assert "16 * 1024" not in source
    assert "16384" not in source
    assert "aiter_raw" not in inspect.getsource(capture_reservation_bytes)
    assert capture_reservation_bytes(RECOVERY_CAPTURE_BYTES) == (
        RECOVERY_CAPTURE_BYTES + CAPTURE_FRAME_OVERHEAD_BYTES
    )
    assert capture_reservation_bytes(RECOVERY_CAPTURE_BYTES) < (
        RECOVERY_CAPTURE_BYTES + 1024 * 1024
    )


@pytest.mark.parametrize("kind", CHUNK_KINDS)
def test_fragmented_4mib_is_fully_retained(tmp_path: Path, kind: str) -> None:
    payload = report_json(FOUR_MIB)
    counted = CountingCrypto(crypto())
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", counted, capture_bytes=RECOVERY_CAPTURE_BYTES)
    stream.announce(http_status=200, content_encoding="identity")
    assert feed_fragmented(stream, payload, kind) is True
    stream.finish(complete=True, http_status=200)
    frames = expected_frames(len(payload))
    assert stream.plaintext_bytes == FOUR_MIB
    assert stream.frame_count == frames
    assert stream.capture_state == CAPTURE_COMPLETE
    assert stream.on_disk_bytes <= stream.reservation.reserved_bytes
    assert stream.on_disk_bytes > stream.plaintext_bytes
    assert counted.data_frame_sizes == [INTERNAL_FRAME_SIZE] * frames
    assert counted.encrypt_calls == frames + CONTROL_FRAME_COUNT
    network_chunks = sum(1 for _ in iter_chunks(payload, kind))
    assert counted.encrypt_calls < network_chunks
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE
    assert recovered[0].plaintext_bytes == FOUR_MIB
    assert recovered[0].payload_sha256
    retained = crypto().decrypt_bound_bytes(
        recovered[0].payload_enc,
        recovered[0].key_version,
        EncryptionContext(
            domain="vendor-raw",
            table="raw_vendor_log",
            column="payload_enc",
            object_id=f"{recovered[0].source}:{recovered[0].payload_sha256}",
        ),
    )
    assert retained == payload
    decoded = decode_pulled_payload(RawPulledPayload(retained, 200), "GetReport")
    assert isinstance(decoded, str)


@pytest.mark.parametrize("kind", CHUNK_KINDS)
def test_fragmented_64mib_is_complete_too_large_not_truncated(
    tmp_path: Path, kind: str
) -> None:
    counted = CountingCrypto(crypto())
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", counted, capture_bytes=RECOVERY_CAPTURE_BYTES)
    stream.announce(http_status=200, content_encoding="identity")
    chunk = b"x"
    if kind == "1":
        for _ in range(RECOVERY_CAPTURE_BYTES):
            assert stream.feed(chunk) is True
    else:
        payload = chunk * RECOVERY_CAPTURE_BYTES
        assert feed_fragmented(stream, payload, kind) is True
    stream.finish(complete=True, too_large=True, http_status=200)
    frames = expected_frames(RECOVERY_CAPTURE_BYTES)
    assert stream.plaintext_bytes == RECOVERY_CAPTURE_BYTES
    assert stream.frame_count == frames
    assert stream.capture_state == CAPTURE_COMPLETE_TOO_LARGE
    assert stream.on_disk_bytes <= stream.reservation.reserved_bytes
    assert counted.encrypt_calls == frames + CONTROL_FRAME_COUNT
    if kind == "1":
        assert counted.encrypt_calls < RECOVERY_CAPTURE_BYTES // 1000
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE_TOO_LARGE
    assert recovered[0].plaintext_bytes == RECOVERY_CAPTURE_BYTES


def test_over_64mib_truncates_only_at_hard_cap(tmp_path: Path) -> None:
    counted = CountingCrypto(crypto())
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", counted, capture_bytes=RECOVERY_CAPTURE_BYTES)
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(b"x" * RECOVERY_CAPTURE_BYTES) is True
    assert stream.feed(b"y" * 17) is False
    stream.finish(complete=False, http_status=200)
    assert stream.plaintext_bytes == RECOVERY_CAPTURE_BYTES
    assert stream.frame_count == expected_frames(RECOVERY_CAPTURE_BYTES)
    assert stream.capture_state == CAPTURE_TRUNCATED
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].plaintext_bytes == RECOVERY_CAPTURE_BYTES


def test_small_feed_does_not_create_a_durable_frame(tmp_path: Path) -> None:
    counted = CountingCrypto(crypto())
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", counted, capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(b"abc") is True
    assert stream.frame_count == 0
    assert stream.pending_plaintext_bytes == 3
    assert stream.plaintext_bytes == 3
    assert counted.data_frame_sizes == []
    assert stream.flush() is True
    assert stream.frame_count == 1
    assert stream.pending_plaintext_bytes == 0
    assert counted.data_frame_sizes == [3]


@pytest.mark.parametrize("kind", CHUNK_KINDS)
@pytest.mark.asyncio
async def test_async_4mib_proxy_fragments_are_parsed(tmp_path: Path, kind: str) -> None:
    payload = report_json(FOUR_MIB)
    body = FragmentStream(payload, kind)
    counted = CountingCrypto(crypto())
    store = RawSpillStore(tmp_path)
    sink = store.open_stream("report", counted, capture_bytes=RECOVERY_CAPTURE_BYTES)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body, request=request)

    client = make_client(handler)
    pulled = await client.get_report_raw(body_sink=sink)
    await client.aclose()
    assert pulled.raw_payload == payload
    decoded = decode_pulled_payload(pulled, "GetReport")
    assert isinstance(decoded, str)
    frames = expected_frames(len(payload))
    assert sink.plaintext_bytes == FOUR_MIB
    assert sink.frame_count == frames
    assert sink.capture_state == CAPTURE_COMPLETE
    assert sink.on_disk_bytes <= sink.reservation.reserved_bytes
    assert counted.encrypt_calls == frames + CONTROL_FRAME_COUNT
    assert counted.encrypt_calls < body.yielded
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE
    assert recovered[0].plaintext_bytes == FOUR_MIB


@pytest.mark.parametrize("kind", ("16", "1024", "random"))
@pytest.mark.asyncio
async def test_async_64mib_fragments_are_complete_too_large(
    tmp_path: Path, kind: str
) -> None:
    payload = b"x" * RECOVERY_CAPTURE_BYTES
    body = FragmentStream(payload, kind)
    counted = CountingCrypto(crypto())
    store = RawSpillStore(tmp_path)
    sink = store.open_stream("report", counted, capture_bytes=RECOVERY_CAPTURE_BYTES)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body, request=request)

    client = make_client(handler)
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_report_raw(body_sink=sink)
    await client.aclose()
    assert captured.value.complete is True
    assert len(captured.value.raw_body) == RECOVERY_CAPTURE_BYTES
    frames = expected_frames(RECOVERY_CAPTURE_BYTES)
    assert sink.plaintext_bytes == RECOVERY_CAPTURE_BYTES
    assert sink.frame_count == frames
    assert sink.capture_state == CAPTURE_COMPLETE_TOO_LARGE
    assert sink.on_disk_bytes <= sink.reservation.reserved_bytes
    assert counted.encrypt_calls == frames + CONTROL_FRAME_COUNT
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE_TOO_LARGE
    assert recovered[0].plaintext_bytes == RECOVERY_CAPTURE_BYTES


@pytest.mark.asyncio
async def test_async_over_64mib_truncates_at_cap(tmp_path: Path) -> None:
    payload = b"z" * (RECOVERY_CAPTURE_BYTES + 4096)
    body = FragmentStream(payload, "1024")
    store = RawSpillStore(tmp_path)
    sink = store.open_stream("reply", crypto(), capture_bytes=RECOVERY_CAPTURE_BYTES)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body, request=request)

    client = make_client(handler)
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_reply_raw(body_sink=sink)
    await client.aclose()
    assert captured.value.complete is False
    assert len(captured.value.raw_body) == RECOVERY_CAPTURE_BYTES
    assert sink.plaintext_bytes == RECOVERY_CAPTURE_BYTES
    assert sink.frame_count == expected_frames(RECOVERY_CAPTURE_BYTES)
    assert sink.capture_state == CAPTURE_TRUNCATED
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].plaintext_bytes == RECOVERY_CAPTURE_BYTES


@pytest.mark.asyncio
async def test_absolute_timeout_flushes_short_frame(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    sink = store.open_stream("report", crypto(), capture_bytes=RECOVERY_CAPTURE_BYTES)

    async def handler(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield b"a" * INTERNAL_FRAME_SIZE
            yield b"b" * 100
            await asyncio.sleep(1)
            yield b"c" * 8

        return httpx.Response(200, content=body(), request=request)

    client = make_client(handler, total_timeout_s=0.05)
    with pytest.raises(VendorTotalTimeout):
        await client.get_report_raw(body_sink=sink)
    await client.aclose()
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].plaintext_bytes == INTERNAL_FRAME_SIZE + 100
    assert sink.frame_count == 2
    assert sink.plaintext_bytes == INTERNAL_FRAME_SIZE + 100


def test_near_quota_still_refuses_unproven_64mib(tmp_path: Path) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 20 * 1024 * 1024)
    padding = tmp_path / "padding.bin"
    with padding.open("wb") as handle:
        handle.truncate(store.max_total_bytes - 63 * 1024 * 1024)
    with pytest.raises(SpillQuotaExceeded):
        store.open_stream("report", crypto())
    assert list(tmp_path.glob("*.reserve")) == []
    assert list(tmp_path.glob("*.stream*")) == []
