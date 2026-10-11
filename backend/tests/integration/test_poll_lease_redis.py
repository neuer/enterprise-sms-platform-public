"""真实 Redis 租期到期与接管；旧持有者不能释放新 token。"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.redis_lock import HeartbeatLock, LeaseUncertain

ROOT = Path(__file__).resolve().parents[3]
REDIS_IMAGE = re.search(r"^FROM (\S+)", (ROOT / "deploy/redis.Dockerfile").read_text()).group(1)

pytestmark = pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="isolated Redis 7")


@pytest.mark.asyncio
async def test_expired_owner_observes_takeover_and_cannot_release_new_lock() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"])
    key = f"test:poll-lease:{uuid4().hex}"
    old = HeartbeatLock(redis.lock(key, timeout=3, thread_local=False), ttl_s=3, beat_s=1)
    new = redis.lock(key, timeout=10, thread_local=False)
    try:
        assert await old.acquire()
        await redis.pexpire(key, 1)
        for _ in range(100):
            if await new.acquire(blocking=False):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("isolated lock did not expire")
        await asyncio.wait_for(old.wait_uncertain(), 2)
        with pytest.raises(LeaseUncertain):
            old.ensure_lease()
        assert not await old.release()
        assert await new.owned()
        await new.release()
        assert await old.acquire()
        old.ensure_lease()
        assert await old.release()
    finally:
        await old.release()
        if await new.owned():
            await new.release()
        await redis.aclose()


@pytest.mark.asyncio
async def test_redis_connection_failure_signals_without_cancelling_work() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"])
    # 仅替换续租连接为本机不可连接端口，实际 owner 保留在隔离 Redis。
    unavailable = Redis(host="127.0.0.1", port=0, socket_connect_timeout=0.1)
    key = f"test:poll-lease:{uuid4().hex}"
    underlying = redis.lock(key, timeout=5, thread_local=False)
    heartbeat = HeartbeatLock(underlying, ttl_s=5, beat_s=1)
    try:
        assert await heartbeat.acquire()
        underlying.redis = unavailable
        await asyncio.wait_for(heartbeat.wait_uncertain(), 3)
        assert heartbeat.lease_uncertain
        underlying.redis = redis
        assert await underlying.owned()
        assert not await heartbeat.release()
        assert not await redis.exists(key)
    finally:
        underlying.redis = redis
        await heartbeat.release()
        await unavailable.aclose()
        await redis.aclose()


@pytest.mark.asyncio
async def test_real_redis_command_pause_bounds_renewal_at_deadline(restartable_redis) -> None:
    """暂停专属 Redis 的命令处理，真实 socket 等待不得越过旧租约。"""
    redis, _ = restartable_redis
    underlying = redis.lock(f"test:pause:{uuid4().hex}", timeout=2, thread_local=False)
    heartbeat = HeartbeatLock(underlying, ttl_s=2, beat_s=1)
    try:
        assert await heartbeat.acquire()
        await redis.execute_command("CLIENT", "PAUSE", 5000, "ALL")
        await asyncio.wait_for(heartbeat.wait_uncertain(), 3)
        assert heartbeat.uncertain_reason == "deadline_elapsed"
        with pytest.raises(LeaseUncertain):
            heartbeat.ensure_lease()
    finally:
        await redis.execute_command("CLIENT", "UNPAUSE")
        assert not await heartbeat.release()


@pytest.fixture
async def restartable_redis():
    """只创建/重启本测试的无持久化 Redis，绝不操作门禁共享容器。"""
    import socket
    import subprocess

    # 固定本次随机回环端口，避免 Docker restart 重新分配动态发布端口。
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        selected_port = reservation.getsockname()[1]
    root = ROOT
    name = f"sms-poll-fault-{uuid4().hex}"
    image = REDIS_IMAGE

    async def docker(*arguments):
        command = ["docker", *arguments]
        if os.environ.get("SMS_DOCKER_PUBLIC_SESSION") != "1":
            command = [str(root / "scripts/docker_public.sh"), "run", "--", *command]
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            check=True, capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()

    client = None
    try:
        await docker(
            "run", "--detach", "--rm", "--name", name,
            "--publish", f"127.0.0.1:{selected_port}:6379", "--tmpfs", "/data",
            image, "redis-server", "--save", "", "--appendonly", "no",
        )
        port = int((await docker("port", name, "6379/tcp")).rsplit(":", 1)[1])
        client = Redis(host="127.0.0.1", port=port, socket_connect_timeout=0.2)

        async def ready():
            for _ in range(50):
                try:
                    if await client.ping():
                        return
                except (RedisConnectionError, OSError):
                    pass
                await asyncio.sleep(0.1)
            pytest.fail("isolated Redis did not become ready")

        async def restart():
            await docker("restart", "--time", "0", name)
            await ready()

        await ready()
        yield client, restart
    finally:
        if client is not None:
            await client.aclose()
        await docker("rm", "--force", name)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["report", "reply"])
@pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ, reason="isolated PostgreSQL required"
)
async def test_restart_takeover_during_capture_preserves_bytes_and_stops_old_worker(
    restartable_redis, tmp_path, source
) -> None:
    from types import SimpleNamespace

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.services.crypto import EncryptionContext
    from app.services.raw_spill import RawSpillStore
    from app.services.reply_ingest import ReplyIngestService
    from app.services.reply_repository import SqlReplyRepository
    from app.services.report_ingest import ReportIngestService
    from app.services.report_repository import SqlReportRepository
    from tests.test_raw_spill_artifact_state import (
        FakeAlerts,
        FakeRepository,
        RecordingGateway,
        crypto,
        empty_payload,
    )

    redis, restart = restartable_redis
    key = f"test:restart:{uuid4().hex}"
    old = HeartbeatLock(redis.lock(key, timeout=3, thread_local=False), ttl_s=3, beat_s=1)
    new = HeartbeatLock(redis.lock(key, timeout=3, thread_local=False), ttl_s=3, beat_s=1)
    cipher = crypto()

    class InterruptedGateway(RecordingGateway):
        async def consume(self, body_sink=None):
            await restart()
            assert await new.acquire()
            await asyncio.wait_for(old.wait_uncertain(), 3)
            # 响应仍在途；仅返回合成字节，不取消已被厂商消费的响应。
            return await super().get_report_raw(body_sink=body_sink)

        get_report_raw = consume
        get_reply_raw = consume

    service_type = ReportIngestService if source == "report" else ReplyIngestService
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository_type = SqlReportRepository if source == "report" else SqlReplyRepository
    saved = []

    class DurableRepository(repository_type):
        async def persist_raw(self, **values):
            raw_id = await super().persist_raw(**values)
            saved.append(raw_id)
            return raw_id

    gateway = InterruptedGateway(empty_payload())
    repository = DurableRepository(SimpleNamespace(database_url=database_url))
    service = service_type(
        gateway, repository, cipher, alerts=FakeAlerts(), spill=RawSpillStore(tmp_path)
    )
    try:
        assert await old.acquire()
        with pytest.raises(LeaseUncertain):
            await service.poll_once(check_lease=old.ensure_lease)
        assert gateway.calls == 1
        assert len(saved) == 1
        # 新连接读取已提交行，不能用仓储入参或未提交事务代替持久化证据。
        async with engine.connect() as connection:
            item = (await connection.execute(text(
                "SELECT payload_enc,payload_sha256,key_version,processed "
                "FROM raw_vendor_log WHERE id=:id"
            ), {"id": saved[0]})).mappings().one()
        assert not item["processed"]
        assert cipher.decrypt_bound_bytes(
            item["payload_enc"], item["key_version"],
            EncryptionContext(
                domain="vendor-raw", table="raw_vendor_log", column="payload_enc",
                object_id=f'{source}:{item["payload_sha256"]}',
            ),
        ) == gateway.result.raw_payload
        assert not await old.release()
        new.ensure_lease()
        fresh_gateway = RecordingGateway(empty_payload())
        fresh = service_type(fresh_gateway, FakeRepository(), cipher)
        await fresh.poll_once(check_lease=new.ensure_lease)
        assert fresh_gateway.calls == 1
        with pytest.raises(LeaseUncertain):
            old.ensure_lease()
        assert gateway.calls == 1
        assert await new.release()
    finally:
        await old.release()
        await new.release()
        async with engine.begin() as connection:
            for raw_id in saved:
                await connection.execute(
                    text("DELETE FROM worker_lease_event WHERE task_kind='raw' AND task_id=:id"),
                    {"id": raw_id},
                )
                await connection.execute(
                    text("DELETE FROM raw_vendor_log WHERE id=:id"), {"id": raw_id}
                )
        await engine.dispose()
