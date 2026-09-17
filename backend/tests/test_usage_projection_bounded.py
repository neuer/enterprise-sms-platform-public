"""真实账本分页、Redis 命令预算与重建 ready 边界的定向回归。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.services import usage_ledger as module
from app.services.usage_ledger import ProjectionRow, UsageLedgerService, UsageProjectionUnavailable
from tests.test_usage_ledger import ProjectionRedis

NOW = datetime(2026, 9, 7, 8, tzinfo=UTC)


class Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self.rows


class PageDatabase:
    def __init__(self, rows: list[ProjectionRow]) -> None:
        self.rows = sorted(rows, key=lambda row: row.dimension_key)
        self.page_sizes: list[int] = []
        self.cursors: list[str] = []
        self.audit: list[dict[str, Any]] = []
        self.locked = False

    def connect(self) -> PageDatabase:
        return self

    def begin(self) -> PageDatabase:
        return self

    async def __aenter__(self) -> PageDatabase:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, sql: object, params: object) -> bool:
        assert "pg_try_advisory_lock" in str(sql)
        if self.locked:
            return False
        self.locked = True
        return True

    async def rollback(self) -> None:
        return None

    async def execute(self, sql: object, params: dict[str, Any] | None = None) -> Result:
        statement = str(sql)
        if "pg_advisory_unlock" in statement:
            self.locked = False
            return Result([])
        if "FROM usage_projection" in statement:
            assert "ORDER BY dimension_key LIMIT :limit" in statement
            assert params is not None and params["limit"] <= module.PROJECTION_BATCH_ROWS
            cursor = params["cursor"]
            rows = [asdict(row) for row in self.rows if row.dimension_key > cursor][
                : params["limit"]
            ]
            self.page_sizes.append(len(rows))
            self.cursors.append(cursor)
            return Result(rows)
        if "INSERT INTO audit_log" in statement:
            self.audit.append(params or {})
        return Result([])


class RecordingRedis(ProjectionRedis):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[str, int, int]] = []
        self.apply_calls = 0
        self.fail_apply_at: int | None = None
        self.before_apply: Any = None

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        self.commands.append(
            ("EVAL", numkeys, module._redis_command_bytes("EVAL", script, numkeys, *args))
        )
        if script == module.APPLY_PROJECTIONS_LUA:
            self.apply_calls += 1
            if self.before_apply is not None:
                await self.before_apply(self.apply_calls)
            if self.apply_calls == self.fail_apply_at:
                raise ConnectionError("synthetic midpage failure")
        return await super().eval(script, numkeys, *args)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.commands.append(("MGET", len(keys), module._redis_command_bytes("MGET", *keys)))
        return await super().mget(keys)


def rows(count: int) -> list[ProjectionRow]:
    return [
        ProjectionRow(
            f"quota:app:{index:06d}:20260907",
            "quota",
            date(2026, 9, 7),
            index + 1,
            index + 1,
            NOW + timedelta(days=1),
        )
        for index in range(count)
    ]


def service_for(database: PageDatabase, redis: RecordingRedis) -> UsageLedgerService:
    service = UsageLedgerService(redis, object(), clock=lambda: NOW)  # type: ignore[arg-type]
    service._engine = lambda: database  # type: ignore[method-assign]
    return service


@pytest.fixture(autouse=True)
def audit_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def bind(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(module, "bind_connection_system_audit", bind)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError, asyncio.CancelledError])
async def test_rebuild_begin_failure_releases_owner_and_preserves_error_contract(
    failure: type[BaseException],
) -> None:
    class FailingBeginRedis(RecordingRedis):
        async def eval(self, script: str, numkeys: int, *args: Any) -> int:
            if script == module.BEGIN_PROJECTION_REBUILD_LUA:
                raise failure("synthetic begin failure")
            return await super().eval(script, numkeys, *args)

    database = PageDatabase(rows(1))
    redis = FailingBeginRedis()
    service = service_for(database, redis)
    expected = UsageProjectionUnavailable if failure is ConnectionError else asyncio.CancelledError
    with pytest.raises(expected) as caught:
        await service.rebuild()
    if failure is ConnectionError:
        assert isinstance(caught.value.__cause__, ConnectionError)
    assert not database.locked
    assert database.page_sizes == []
    assert database.audit == []
    assert not any(key.startswith("usage:projection:ready:") for key in redis.values)


@pytest.mark.asyncio
async def test_rebuild_and_drift_bound_database_pages_and_redis_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "PROJECTION_BATCH_ROWS", 7)
    monkeypatch.setattr(module, "PROJECTION_COMMAND_BYTES", 1500)
    database = PageDatabase(rows(24))
    redis = RecordingRedis()
    service = service_for(database, redis)

    assert await service.rebuild() == 24
    assert database.page_sizes == [7, 7, 7, 3]
    assert database.cursors == [
        "",
        database.rows[6].dimension_key,
        database.rows[13].dimension_key,
        database.rows[20].dimension_key,
    ]
    assert database.audit[0]["dimension_count"] == 24
    assert not database.locked
    assert module.PROJECTION_REBUILD_KEY not in redis.values
    assert redis.values["usage:projection:ready:20260907"] == "1"
    # Byte budget is deliberately lower than seven rows to exercise both bounds.
    assert redis.apply_calls > len(database.page_sizes)
    drift = await service.measure_drift()
    assert drift.mismatches == 0
    assert all(size <= 1500 for _, _, size in redis.commands)
    assert all(keys <= 14 for _, keys, _ in redis.commands)


@pytest.mark.asyncio
async def test_rebuild_midpage_failure_keeps_not_ready_and_retry_is_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "PROJECTION_BATCH_ROWS", 2)
    database = PageDatabase(rows(5))
    redis = RecordingRedis()
    service = service_for(database, redis)
    ready = "usage:projection:ready:20260907"
    redis.values[ready] = "1"
    redis.fail_apply_at = 2

    async def check_not_ready(_call: int) -> None:
        assert ready not in redis.values
        with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
            await service.ensure_ready(NOW)
        with pytest.raises(UsageProjectionUnavailable):
            await service._publish_ready({date(2026, 9, 7): NOW + timedelta(days=1)})

    redis.before_apply = check_not_ready
    with pytest.raises(UsageProjectionUnavailable):
        await service.rebuild()
    assert ready not in redis.values
    assert module.PROJECTION_REBUILD_KEY in redis.values
    assert not database.audit
    assert not database.locked
    # An ordinary concurrent release/apply must not publish ready either.
    redis.before_apply = None
    redis.fail_apply_at = None
    newer = ProjectionRow(
        database.rows[0].dimension_key, "quota", date(2026, 9, 7), 91, 91, NOW + timedelta(days=1)
    )
    await service._apply_rows([newer])
    assert ready not in redis.values
    assert await service.rebuild() == 5
    assert redis.values[newer.dimension_key] == "91"
    assert redis.values[database.rows[1].dimension_key] == "2"
    assert redis.values[ready] == "1"


@pytest.mark.asyncio
async def test_rebuild_lost_redis_barrier_never_publishes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "PROJECTION_BATCH_ROWS", 2)
    database = PageDatabase(rows(3))
    redis = RecordingRedis()
    service = service_for(database, redis)

    async def lose_barrier(call: int) -> None:
        if call == 1:
            redis.values.pop(module.PROJECTION_REBUILD_KEY)

    redis.before_apply = lose_barrier
    with pytest.raises(UsageProjectionUnavailable, match="owner lost"):
        await service.rebuild()
    assert not any(key.startswith("usage:projection:ready:") for key in redis.values)
    assert not database.locked


@pytest.mark.asyncio
async def test_cross_day_rows_publish_only_after_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "PROJECTION_BATCH_ROWS", 1)
    first = rows(1)[0]
    second = ProjectionRow(
        "quota:app:999999:20260908", "quota", date(2026, 9, 8), 7, 7, NOW + timedelta(days=2)
    )
    database = PageDatabase([first, second])
    redis = RecordingRedis()
    service = service_for(database, redis)

    async def check_not_ready(_call: int) -> None:
        assert not any(key.startswith("usage:projection:ready:") for key in redis.values)

    redis.before_apply = check_not_ready
    assert await service.rebuild() == 2
    assert redis.values["usage:projection:ready:20260907"] == "1"
    assert redis.values["usage:projection:ready:20260908"] == "1"


@pytest.mark.asyncio
async def test_cross_page_late_low_key_commit_is_fenced_before_failed_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已通过准入的旧请求也不能在游标后提交低 key，再漏写 Redis。"""

    monkeypatch.setattr(module, "PROJECTION_BATCH_ROWS", 2)
    original = rows(4)
    database = PageDatabase(original)
    redis = RecordingRedis()
    service = service_for(database, redis)
    low = ProjectionRow(
        "quota:app:000000:20260906", "quota", date(2026, 9, 7), 17, 100, NOW + timedelta(days=1)
    )
    committed = []
    rejected = []

    class LateWriter:
        async def scalar(self, sql: object, params: dict[str, Any]) -> bool:
            assert "pg_try_advisory_xact_lock_shared" in str(sql)
            assert params == {"name": "usage:projection:rebuild"}
            return not database.locked

        async def execute(self, sql: object, params: dict[str, Any]) -> Result:
            assert "INSERT INTO usage_projection" in str(sql)
            # This is the counterexample if the production writer gate is removed:
            # a committed key sorts before the cursor, then its Redis write fails.
            database.rows.insert(0, low)
            committed.append(low.dimension_key)
            return Result([asdict(low)])

    async def commit_then_fail_projection(call: int) -> None:
        if call != 2:
            return
        assert database.cursors[-1] == original[1].dimension_key
        try:
            changed = await module._change_projections(
                LateWriter(),  # type: ignore[arg-type]
                [
                    module._ProjectionChange(
                        low.dimension_key, "quota", low.usage_date, "20260907", 17, low.expires_at
                    )
                ],
            )
        except UsageProjectionUnavailable:
            rejected.append(True)
            return
        redis.fail = True
        try:
            await service._apply_rows(changed)
        finally:
            redis.fail = False

    redis.before_apply = commit_then_fail_projection
    assert await service.rebuild() == 4
    assert rejected == [True]
    assert committed == []
    assert low.dimension_key not in redis.values
    assert redis.values["usage:projection:ready:20260907"] == "1"
    assert [int(redis.values[row.dimension_key]) for row in original] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_existing_writer_prevents_rebuild_snapshot_and_ready_publication() -> None:
    database = PageDatabase(rows(2))
    database.locked = True
    redis = RecordingRedis()
    service = service_for(database, redis)
    with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
        await service.rebuild()
    assert database.page_sizes == []
    assert not any(key.startswith("usage:projection:ready:") for key in redis.values)


@pytest.mark.asyncio
async def test_failed_page_rolls_back_before_releasing_pooled_session_lock() -> None:
    class AbortedDatabase(PageDatabase):
        aborted = False
        rolled_back = False

        async def execute(self, sql: object, params: dict[str, Any] | None = None) -> Result:
            if "FROM usage_projection" in str(sql):
                self.aborted = True
                raise RuntimeError("synthetic transaction abort")
            assert not self.aborted, "unlock must not run in an aborted transaction"
            return await super().execute(sql, params)

        async def rollback(self) -> None:
            self.aborted = False
            self.rolled_back = True

    database = AbortedDatabase(rows(1))
    redis = RecordingRedis()
    service = service_for(database, redis)
    with pytest.raises(UsageProjectionUnavailable, match="rebuild unavailable"):
        await service.rebuild()
    assert database.rolled_back
    assert not database.locked
    assert not any(key.startswith("usage:projection:ready:") for key in redis.values)
