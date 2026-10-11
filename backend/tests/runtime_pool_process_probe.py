"""独立进程中的真实 PostgreSQL prefork 池探针；仅输出非敏感布尔结论。"""
from __future__ import annotations

import asyncio
import gc
import multiprocessing
import os
from multiprocessing.connection import Connection

from sqlalchemy import text
from sqlalchemy.engine import make_url

import app.core.runtime_resources as resources


def _engine() -> resources.ManagedAsyncEngine:
    return resources.database_engine(
        make_url(os.environ["OUTBOX_POSTGRES_DSN"]), component="background",
    )


async def _pid() -> int:
    async with _engine().connect() as connection:
        return int(await connection.scalar(text("SELECT pg_backend_pid()")))


def _worker(channel: Connection) -> None:
    resources.discard_inherited_runtime_resources()
    gc.collect()
    print("prefork-phase:child-discarded", flush=True)

    async def probe() -> tuple[set[int], bool]:
        engine = _engine()
        assert _engine() is engine

        async def load() -> int:
            async with engine.connect() as connection:
                pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
                await connection.execute(text("SELECT pg_sleep(0.03)"))
                return pid

        try:
            pids = set(await asyncio.gather(*(load() for _ in range(6))))
            await engine.dispose()
            assert _engine() is engine
            assert await _pid() in pids
            assert len(pids) <= 2
            print("prefork-phase:child-pool-exercised", flush=True)
        finally:
            await resources.close_runtime_resources()
        snapshot = resources.resource_snapshot()
        closed = all(
            item.open == item.checked_out == item.leaked_on_shutdown == 0
            for item in snapshot.database_components
        )
        print("prefork-phase:child-resources-closed", flush=True)
        return pids, closed

    try:
        channel.send(asyncio.run(probe()))
        print("prefork-phase:child-result-sent", flush=True)
    except BaseException:
        channel.send((set(), False))
        raise
    finally:
        channel.close()


def main() -> None:
    resources._BUDGETS["background"] = resources.DatabasePoolBudget(
        pool_size=2, max_overflow=0, pool_timeout_seconds=2,
        connect_timeout_seconds=2, statement_timeout_ms=5_000,
    )
    loop = asyncio.new_event_loop()
    context = multiprocessing.get_context("fork")
    children: list[multiprocessing.Process] = []
    readers: list[Connection] = []
    try:
        parent_pid = loop.run_until_complete(_pid())
        print("prefork-phase:parent-warmed", flush=True)
        for _ in range(2):
            reader, writer = context.Pipe(duplex=False)
            process = context.Process(target=_worker, args=(writer,))
            process.start()
            writer.close()
            readers.append(reader)
            children.append(process)
        print("prefork-phase:workers-started", flush=True)
        results = []
        for reader in readers:
            assert reader.poll(20), "worker resource probe timed out"
            results.append(reader.recv())
        print("prefork-phase:results-received", flush=True)
        for process in children:
            process.join(timeout=5)
            assert process.exitcode == 0, "worker resource probe failed"
        assert all(pids and closed and parent_pid not in pids for pids, closed in results)
        assert results[0][0].isdisjoint(results[1][0])
        # 子进程 discard/close 没有关闭父进程的既有数据库连接。
        assert loop.run_until_complete(_pid()) == parent_pid
    finally:
        for process in children:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
        for reader in readers:
            reader.close()
        loop.run_until_complete(resources.close_runtime_resources())
        loop.close()
    print("prefork-pools: isolated, bounded, parent-preserved, closed")


if __name__ == "__main__":
    main()
