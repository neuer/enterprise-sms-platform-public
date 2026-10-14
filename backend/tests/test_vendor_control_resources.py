"""联调代理容量、线程取消、单次会话缓存与真实 Linux UDS 边界。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from time import monotonic, process_time
from uuid import uuid4

import pytest

from tests.test_vendor_control_agent import FakeCredentialStore, FakeRunner
from tests.test_vendor_seal_sessions import NOW, _seal

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "scripts"))
import vendor_control_agent as agent_module
import vendor_seal_sessions as seal

from vendor_control_protocol import (
    MAX_FRAME_BYTES,
    ControlRequest,
    decode_response,
    encode_request,
)

ROOT = Path(__file__).resolve().parents[2]


async def until(predicate) -> None:
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    timer = None

    def check():
        nonlocal timer
        if predicate():
            event.set()
        else:
            timer = loop.call_later(0.005, check)

    check()
    try:
        async with asyncio.timeout(3):
            await event.wait()
    finally:
        if timer is not None:
            timer.cancel()


def make_agent(manager):
    return agent_module.VendorControlAgent(
        runner=FakeRunner(),
        credential_store=FakeCredentialStore(),
        seal_sessions=manager,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        status_only_uid=os.getuid(),
        status_only_gid=os.getgid() + 1,
    )


def create_request(actor="admin", operation_id=None):
    return ControlRequest(
        operation_id or str(uuid4()),
        "create_seal_session",
        {"operation": "install_credentials", "actor": actor},
    )


def invoke(agent, request):
    return agent.handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())


def test_seal_response_cache_never_revives_consumed_expired_or_cross_actor_session() -> None:
    now = [NOW]
    manager = seal.SealSessionManager(clock=lambda: now[0])
    agent = make_agent(manager)
    request = create_request()
    response = invoke(agent, request)
    assert response.status == "ok"
    assert invoke(agent, request) == response
    assert (
        invoke(agent, create_request("other", request.operation_id)).safe_code
        == "OPERATION_ID_CONFLICT"
    )
    session = seal.SealSession(
        response.body["session_id"],
        response.body["public_key"],
        NOW + timedelta(seconds=120),
        response.body["aad"],
    )
    manager.open(_seal(seal, session), operation="install_credentials", actor="admin")
    assert invoke(agent, request).safe_code == "CONTROL_OPERATION_REJECTED"
    later = create_request()
    assert invoke(agent, later).status == "ok"
    now[0] += timedelta(seconds=121)
    assert invoke(agent, later).safe_code == "CONTROL_OPERATION_REJECTED"


def test_cancelled_replay_cannot_discard_previously_delivered_session() -> None:
    manager = seal.SealSessionManager()
    agent = make_agent(manager)
    request = create_request()
    first = agent_module._ResultDelivery(agent, request)
    response = first.complete(invoke(agent, request))
    first.begin()
    replay = agent_module._ResultDelivery(agent, request)
    replay.complete(invoke(agent, request))
    replay.abandon()
    assert manager.is_active(
        response.body["session_id"], operation="install_credentials", actor="admin"
    )
    assert invoke(agent, request) == response


@pytest.mark.asyncio
async def test_response_drain_and_close_wait_have_separate_bounded_deadlines() -> None:
    limits = replace(seal.AgentLimits(), write_timeout_s=0.02, close_timeout_s=0.02)
    resources = agent_module.ControlServer(make_agent(seal.SealSessionManager()), limits)
    reader = asyncio.StreamReader()
    reader.feed_data(encode_request(ControlRequest(str(uuid4()), "health", {})))

    class SlowWriter:
        closed = False
        response = b""

        def write(self, response):
            self.response = response

        async def drain(self):
            await asyncio.Event().wait()

        def close(self):
            self.closed = True

        async def wait_closed(self):
            await asyncio.Event().wait()

    writer = SlowWriter()
    try:
        await asyncio.wait_for(
            agent_module._serve_client(
                resources,
                reader,
                writer,
                uid=os.getuid(),
                gid=os.getgid(),
            ),
            0.5,
        )
        assert writer.closed and decode_response(writer.response).status == "ok"
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_executor_cancel_keeps_running_and_queued_capacity_until_thread_finishes() -> None:
    entered, release = Event(), Event()
    executor = agent_module.AgentExecutor(1, 2, "test-bounded")

    def blocked():
        entered.set()
        assert release.wait(3)
        return 1

    first = asyncio.create_task(executor.run(blocked))
    second = None
    try:
        await until(entered.is_set)
        second = asyncio.create_task(executor.run(lambda: 2))
        await until(lambda: executor.pending == 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(agent_module.ExecutorFull):
            await executor.run(lambda: 3)
        assert executor.pending == executor.peak_pending == 2
        release.set()
        assert await second == 2
        await until(lambda: executor.pending == 0)
        assert await executor.run(lambda: 4) == 4
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        executor.close()


@pytest.mark.asyncio
async def test_periodic_cleanup_runs_without_new_create() -> None:
    now = [NOW]
    manager = seal.SealSessionManager(clock=lambda: now[0])
    manager.create("install_credentials", "admin")
    limits = replace(seal.AgentLimits(), cleanup_interval_s=0.01)
    task = asyncio.create_task(agent_module.session_cleanup_loop(manager, limits))
    try:
        await asyncio.sleep(0)
        assert len(manager._sessions) == 1
        now[0] += timedelta(seconds=121)
        await until(lambda: not manager._sessions)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@asynccontextmanager
async def uds(tmp_path, *, limits=None, manager=None):
    manager = manager or seal.SealSessionManager(limits=limits)
    agent = make_agent(manager)
    resources = agent_module.ControlServer(agent, limits)
    path = tmp_path / "control.sock"
    server = await asyncio.start_unix_server(resources.accept, path=path, limit=MAX_FRAME_BYTES + 4)
    path.chmod(0o666)  # 仅隔离临时 socket，用真实不同 GID 验证应用层拒绝矩阵。
    try:
        yield path, agent, resources, manager
    finally:
        server.close()
        await server.wait_closed()
        await resources.close()


async def exchange(path, request):
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(encode_request(request))
        await writer.drain()
        async with asyncio.timeout(3):
            header = await reader.readexactly(4)
            size = struct.unpack("!I", header)[0]
            return decode_response(header + await reader.readexactly(size))
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


LINUX = pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="requires Linux SO_PEERCRED")


@LINUX
@pytest.mark.asyncio
async def test_uds_incomplete_frames_absolute_deadline_and_connection_cap(tmp_path) -> None:
    limits = replace(seal.AgentLimits(), api_connections=2, frame_timeout_s=0.15)
    async with uds(tmp_path, limits=limits) as (path, _agent, resources, _manager):
        readers, writers = [], []
        try:
            for _ in range(2):
                reader, writer = await asyncio.open_unix_connection(path)
                readers.append(reader)
                writers.append(writer)
            await until(lambda: resources.connections["api"] == 2)
            excess_r, excess_w = await asyncio.open_unix_connection(path)
            writers.append(excess_w)
            assert await asyncio.wait_for(excess_r.read(), 0.1) == b""
            assert len(resources.tasks) == 2
            writers[0].write(b"\x00")
            writers[1].write(struct.pack("!I", 100) + b"{")
            start = monotonic()
            for _ in range(3):
                await asyncio.sleep(0.035)
                writers[1].write(b" ")
                await writers[1].drain()
            assert await asyncio.wait_for(readers[1].read(), 0.3) == b""
            assert monotonic() - start < 0.35
            assert await asyncio.wait_for(readers[0].read(), 0.3) == b""
            await until(lambda: not resources.tasks)
            reader, writer = await asyncio.open_unix_connection(path)
            writers.append(writer)
            writer.write(struct.pack("!I", MAX_FRAME_BYTES + 1))
            assert await asyncio.wait_for(reader.read(), 0.1) == b""
            await until(lambda: not resources.tasks)
            assert resources.peak_connections == 2
        finally:
            for writer in writers:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()


@LINUX
@pytest.mark.asyncio
async def test_uds_real_rsa_and_cancelled_delivery_retains_thread_slot(
    tmp_path, monkeypatch
) -> None:
    entered, release = Event(), Event()
    limits = replace(seal.AgentLimits(), crypto_workers=1, crypto_pending=1)
    manager = seal.SealSessionManager(limits=limits)
    generate = seal.rsa.generate_private_key

    def slow_generate(**kwargs):
        entered.set()
        assert release.wait(5)
        return generate(**kwargs)

    monkeypatch.setattr(seal.rsa, "generate_private_key", slow_generate)
    async with uds(tmp_path, limits=limits, manager=manager) as (path, agent, resources, _):
        request = create_request()
        call = asyncio.create_task(exchange(path, request))
        try:
            await until(entered.is_set)
            # 同 operation 并发不能再次生成，也不能用其它主体读取公钥。
            assert invoke(agent, request).safe_code == "CONTROL_OPERATION_IN_PROGRESS"
            assert (
                invoke(agent, create_request("other", request.operation_id)).safe_code
                == "OPERATION_ID_CONFLICT"
            )
            response = await exchange(path, create_request("other"))
            assert response.safe_code == "CONTROL_OPERATION_REJECTED"
            # 取消连接任务；已开始的线程仍占执行槽，晚到结果明确无人交付。
            for task in tuple(resources.tasks):
                task.cancel()
            await until(lambda: not resources.tasks)
            assert resources.crypto.pending == 1
            response = await exchange(path, create_request("third"))
            assert response.safe_code == "CONTROL_OPERATION_REJECTED"
            release.set()
            await until(lambda: resources.crypto.pending == 0)
            assert not manager._sessions and not manager._reserved
            assert invoke(agent, request).safe_code == "CONTROL_OPERATION_REJECTED"
            monkeypatch.setattr(seal.rsa, "generate_private_key", generate)
            result = await exchange(path, create_request())
            assert result.status == "ok"
            session = seal.SealSession(
                result.body["session_id"], result.body["public_key"], NOW, result.body["aad"]
            )
            envelope = _seal(seal, session)
            body = {name: getattr(envelope, name) for name in envelope.__dataclass_fields__}
            body["actor"] = "admin"
            installed = await exchange(
                path, ControlRequest(str(uuid4()), "install_credentials", body)
            )
            assert installed.status == "ok"
            assert not manager._sessions
        finally:
            release.set()
            await asyncio.gather(call, return_exceptions=True)
            await until(lambda: resources.crypto.pending == 0)


PEER_CLIENT = """
import socket,struct,sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.settimeout(2)
s.connect(sys.argv[1])
frame=sys.stdin.buffer.read()
if frame: s.sendall(frame)
try:
    header=s.recv(4)
    if header:
        size=struct.unpack('!I',header)[0]
        payload=b''
        while len(payload)<size: payload+=s.recv(size-len(payload))
        sys.stdout.buffer.write(header+payload)
finally: s.close()
"""


async def peer_exchange(path, request, gid):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        PEER_CLIENT,
        str(path),
        group=gid,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(
        process.communicate(
            b"" if request is None else encode_request(request),
        ),
        3,
    )
    assert process.returncode == 0 and not error
    return decode_response(output) if output else None


@LINUX
@pytest.mark.asyncio
async def test_uds_real_peer_matrix_and_reserved_status_under_saturation(
    tmp_path, monkeypatch
) -> None:
    """测量本进程 CPU/RSS/FD 与独立控制路径时延，不推断生产容量。"""
    if os.geteuid() != 0:
        # CI 普通用户不能切换真实 GID；在已有隔离入口内执行同一真实身份矩阵。
        # 容器内 root 直接走下方断言，缺少 Docker 或执行失败均不得变成 skip。
        result = await asyncio.to_thread(
            subprocess.run,
            ["bash", str(ROOT / "scripts/verify_vendor_control_uds.sh")],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "VENDOR_CONTROL_RESOURCE " in result.stdout
        return
    entered, release = Event(), Event()
    limits = replace(
        seal.AgentLimits(),
        api_connections=2,
        frame_timeout_s=1.0,
        crypto_workers=1,
        crypto_pending=1,
    )
    generate = seal.rsa.generate_private_key
    heartbeat_calls = []

    def slow_generate(**kwargs):
        entered.set()
        assert release.wait(5)
        return generate(**kwargs)

    monkeypatch.setattr(seal.rsa, "generate_private_key", slow_generate)
    cpu_start, wall_start = process_time(), monotonic()
    async with uds(tmp_path, limits=limits) as (path, agent, resources, _manager):
        # 不允许身份无需发送任何帧即可被拒；只读身份不能创建密钥。
        assert await peer_exchange(path, None, os.getgid() + 2) is None
        assert await peer_exchange(path, create_request(), os.getgid() + 1) is None
        request = create_request()
        busy = asyncio.create_task(exchange(path, request))
        await until(entered.is_set)
        slow_r, slow_w = await asyncio.open_unix_connection(path)
        del slow_r
        await until(lambda: resources.connections["api"] == 2)

        class Heartbeat:
            def write_heartbeat(self):
                heartbeat_calls.append(monotonic())

        async def quick_interval(_seconds):
            await asyncio.sleep(0.02)

        pulse = asyncio.create_task(
            agent_module.heartbeat_loop(
                Heartbeat(),
                executor=resources.heartbeat,
                sleeper=quick_interval,
            )
        )
        try:
            latencies = []
            for _ in range(4):
                started = monotonic()
                result = await peer_exchange(
                    path,
                    ControlRequest(str(uuid4()), "health", {}),
                    os.getgid() + 1,
                )
                latencies.append(monotonic() - started)
                assert result is not None and result.status == "ok"
            assert max(latencies) < 3
            assert len(heartbeat_calls) >= 2
            intervals = [b-a for a, b in zip(heartbeat_calls, heartbeat_calls[1:], strict=False)]
            assert max(intervals) < 0.5
            status = await asyncio.to_thread(Path("/proc/self/status").read_text)
            rss_kib = int(
                next(line for line in status.splitlines() if line.startswith("VmRSS:")).split()[1]
            )
            print(
                "VENDOR_CONTROL_RESOURCE "
                + json.dumps(
                    {
                        "cpu_seconds": round(process_time() - cpu_start, 4),
                        "elapsed_seconds": round(monotonic() - wall_start, 4),
                        "rss_kib": rss_kib,
                        "open_fds": len(await asyncio.to_thread(os.listdir, "/proc/self/fd")),
                        "peak_connections": resources.peak_connections,
                        "peak_crypto_pending": resources.crypto.peak_pending,
                        "max_status_latency_seconds": round(max(latencies), 4),
                        "heartbeat_samples": len(heartbeat_calls),
                    },
                    sort_keys=True,
                )
            )
        finally:
            release.set()
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            slow_w.close()
            with suppress(OSError):
                await slow_w.wait_closed()
            await busy
