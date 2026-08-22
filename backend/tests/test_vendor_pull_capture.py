from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.vendor.zhihui import (  # noqa: E402
    VendorProtocolError,
    VendorResponseTooLarge,
    ZhihuiClient,
)


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def make_client(
    transport: httpx.AsyncBaseTransport,
    *,
    process_limit: int = 16,
    capture_limit: int = 64,
) -> ZhihuiClient:
    return ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(transport=transport, base_url="http://vendor.test"),
        max_response_body_bytes=process_limit,
        max_response_capture_bytes=capture_limit,
        total_timeout_s=1,
    )


@pytest.mark.asyncio
async def test_report_over_processing_limit_preserves_complete_response() -> None:
    chunks = [b'{"code":0,"data":[', b"x" * 20, b"y" * 20, b"]}"]
    stream = RecordingStream(chunks)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_report_raw()
    await client.aclose()

    assert captured.value.raw_body == b"".join(chunks)
    assert captured.value.complete is True
    assert stream.yielded == len(chunks)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_first_reply_chunk_over_processing_limit_is_not_lost() -> None:
    raw = b"z" * 48
    stream = RecordingStream([raw])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_reply_raw()
    await client.aclose()

    assert captured.value.raw_body == raw
    assert captured.value.complete is True


@pytest.mark.asyncio
async def test_declared_oversize_pull_still_reads_complete_body() -> None:
    raw = b"a" * 40
    stream = RecordingStream([raw[:10], raw[10:]])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(len(raw))},
            stream=stream,
            request=request,
        )

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_report_raw()
    await client.aclose()

    assert captured.value.raw_body == raw
    assert captured.value.complete is True
    assert stream.yielded == 2


@pytest.mark.asyncio
async def test_recovery_capture_limit_is_bounded_and_explicitly_partial() -> None:
    chunks = [b"a" * 32, b"b" * 40, b"c" * 8]
    stream = RecordingStream(chunks)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    client = make_client(httpx.MockTransport(handler), capture_limit=64)
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_reply_raw()
    await client.aclose()

    assert captured.value.raw_body == b"a" * 32 + b"b" * 32
    assert len(captured.value.raw_body) == 64
    assert captured.value.complete is False
    assert stream.yielded == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_non_consuming_endpoint_keeps_pre_read_hard_limit() -> None:
    raw = b"a" * 40
    stream = RecordingStream([raw])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(len(raw))},
            stream=stream,
            request=request,
        )

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_balance()
    await client.aclose()

    assert captured.value.raw_body == b""
    assert captured.value.complete is False
    assert stream.yielded == 0


@pytest.mark.asyncio
async def test_normal_pull_contract_is_unchanged() -> None:
    raw = b'{"code":0,"msg":null,"data":[]}'
    stream = RecordingStream([raw])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    client = make_client(httpx.MockTransport(handler), process_limit=64, capture_limit=128)
    pulled = await client.get_report_raw()
    await client.aclose()

    assert pulled.raw_payload == raw
    assert pulled.status_code == 200
    assert stream.closed is True


class RecordingSink:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.finished: tuple[bool, bool] | None = None
        self.announced: dict[str, object] | None = None
        self.finish_meta: dict[str, object] = {}

    def feed(self, chunk: bytes) -> bool:
        self.chunks.append(chunk)
        return True

    def announce(self, **values: object) -> None:
        self.announced = values

    def finish(self, *, complete: bool, too_large: bool = False, **values: object) -> None:
        self.finished = (complete, too_large)
        self.finish_meta = values


@pytest.mark.asyncio
async def test_consume_path_feeds_encrypted_ready_sink() -> None:
    raw = b'{"code":0,"data":[]}'
    stream = RecordingStream([raw])
    sink = RecordingSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    client = make_client(httpx.MockTransport(handler), process_limit=64, capture_limit=128)
    pulled = await client.get_report_raw(body_sink=sink)
    await client.aclose()

    assert pulled.raw_payload == raw
    assert sink.chunks == [raw]
    assert sink.finished == (True, False)
    assert sink.announced is not None
    assert sink.announced["http_status"] == 200
    assert sink.finish_meta["http_status"] == 200
    assert sink.finish_meta["content_encoding"] == "identity"


@pytest.mark.asyncio
async def test_malformed_content_length_still_captures_consume_body() -> None:
    raw = b'{"code":0,"msg":null,"data":[]}'
    stream = RecordingStream([raw])
    sink = RecordingSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            204,
            headers={"content-length": "not-a-number", "content-encoding": "gzip"},
            stream=stream,
            request=request,
        )

    client = make_client(httpx.MockTransport(handler), process_limit=64, capture_limit=128)
    pulled = await client.get_report_raw(body_sink=sink)
    await client.aclose()

    assert pulled.raw_payload == raw
    assert pulled.status_code == 204
    assert pulled.content_encoding == "unsupported"
    assert pulled.protocol_invalid is True
    assert stream.yielded == 1
    assert sink.announced is not None
    assert sink.announced["protocol_invalid"] is True
    assert sink.announced["http_status"] == 204
    assert sink.finish_meta["protocol_invalid"] is True
    assert sink.finish_meta["http_status"] == 204


@pytest.mark.asyncio
async def test_negative_content_length_still_captures_consume_body() -> None:
    raw = b'{"code":0,"data":[]}'
    stream = RecordingStream([raw])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-length": "-3"},
            stream=stream,
            request=request,
        )

    client = make_client(httpx.MockTransport(handler), process_limit=64, capture_limit=128)
    pulled = await client.get_reply_raw()
    await client.aclose()

    assert pulled.raw_payload == raw
    assert pulled.status_code == 429
    assert pulled.protocol_invalid is True
    assert stream.yielded == 1


@pytest.mark.asyncio
async def test_non_consuming_malformed_content_length_still_fails_closed() -> None:
    stream = RecordingStream([b"{}"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "abc"},
            stream=stream,
            request=request,
        )

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(VendorProtocolError, match="content-length"):
        await client.get_balance()
    await client.aclose()
    assert stream.yielded == 0


def test_capture_limit_must_cover_processing_limit() -> None:
    with pytest.raises(ValueError, match="capture must cover body limit"):
        make_client(
            httpx.MockTransport(lambda request: httpx.Response(200)),
            process_limit=64,
            capture_limit=32,
        )
