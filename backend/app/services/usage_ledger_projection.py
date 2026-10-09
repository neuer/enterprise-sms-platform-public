"""Redis 绝对值投影：版本化写入、批量与命令体积约束、单写者重建与漂移巡检。"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import bind_connection_system_audit, database_engine
from app.services.usage_ledger_common import (
    UsageDrift,
    UsageProjectionUnavailable,
    UsageRedis,
    UsageReservationConflict,
    shanghai_day,
)
from app.settings import Settings

APPLY_PROJECTION_LUA = """
local current_version = tonumber(redis.call('GET', KEYS[2]) or '-1')
local incoming_version = tonumber(ARGV[2])
if current_version > incoming_version then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'PXAT', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'PXAT', ARGV[3])
return 1
"""


APPLY_PROJECTIONS_LUA = """
local applied = 0
for key_index = 1, #KEYS, 2 do
  local row_index = ((key_index - 1) / 2) * 3
  local current_version = tonumber(redis.call('GET', KEYS[key_index + 1]) or '-1')
  local incoming_version = tonumber(ARGV[row_index + 2])
  if current_version <= incoming_version then
    redis.call(
      'SET', KEYS[key_index], ARGV[row_index + 1],
      'PXAT', ARGV[row_index + 3]
    )
    redis.call(
      'SET', KEYS[key_index + 1], ARGV[row_index + 2],
      'PXAT', ARGV[row_index + 3]
    )
    applied = applied + 1
  end
end
return applied
"""


PROJECTION_BATCH_ROWS = 500


PROJECTION_COMMAND_BYTES = 256 * 1024


PROJECTION_REBUILD_KEY = "usage:projection:rebuilding"


PROJECTION_REBUILD_TTL_S = 300


BEGIN_PROJECTION_REBUILD_LUA = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('DEL', KEYS[2])
return 1
"""


RENEW_PROJECTION_REBUILD_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


PUBLISH_PROJECTION_READY_LUA = """
local owner = redis.call('GET', KEYS[1])
if ARGV[1] == '' then
  if owner then return 0 end
elseif owner ~= ARGV[1] then
  return 0
end
for i = 2, #KEYS do
  redis.call('SET', KEYS[i], '1', 'PXAT', ARGV[i])
end
if ARGV[1] ~= '' then redis.call('DEL', KEYS[1]) end
return 1
"""


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    dimension_key: str
    kind: str
    usage_date: date
    value: int
    version: int
    expires_at: datetime


def _redis_command_bytes(*arguments: object) -> int:
    """计算 RESP 数组编码大小，命令和 Lua 正文同样计入预算。"""

    total = len(str(len(arguments))) + 3
    for argument in arguments:
        size = len(str(argument).encode("utf-8"))
        total += size + len(str(size)) + 5
    return total


def _projection_arguments(row: ProjectionRow) -> tuple[str, ...]:
    return (
        row.dimension_key,
        _version_key(row.dimension_key),
        str(row.value),
        str(row.version),
        str(int(row.expires_at.timestamp() * 1000)),
    )


def _projection_batches(rows: Sequence[ProjectionRow]) -> Iterator[list[ProjectionRow]]:
    """以行数和最坏 EVAL 编码字节双界分块，不扩大完整输入副本。"""

    batch: list[ProjectionRow] = []
    overhead = _redis_command_bytes("EVAL", APPLY_PROJECTIONS_LUA, PROJECTION_BATCH_ROWS * 2)
    size = overhead
    for row in rows:
        # 每行五个 bulk 参数；预留数组长度位数增长的余量。
        row_bytes = _redis_command_bytes(*_projection_arguments(row)) + 16
        if overhead + row_bytes > PROJECTION_COMMAND_BYTES:
            raise UsageProjectionUnavailable("usage projection dimension exceeds command budget")
        if batch and (
            len(batch) >= PROJECTION_BATCH_ROWS or size + row_bytes > PROJECTION_COMMAND_BYTES
        ):
            yield batch
            batch = []
            size = overhead
        batch.append(row)
        size += row_bytes
    if batch:
        yield batch


@dataclass(frozen=True, slots=True)
class _ProjectionChange:
    dimension_key: str
    kind: str
    usage_date: date
    window_key: str
    delta: int
    expires_at: datetime
    reset_on_window_change: bool = True


# 必须已在 enforce_live_audit_principal 的 sms_send+realtime/bulk 白名单中；
# 新 actor 名需要手写迁移，不能只改 Python。
RECONCILE_REBUILD_ACTOR = "system:usage-projection-auto"


class UsageReconcileLedger(Protocol):
    async def recover_orphans(self, *, older_than_seconds: int = 600) -> int: ...

    async def measure_drift(self) -> UsageDrift: ...

    async def rebuild(self, *, actor: str = "system:usage-projection") -> int: ...


async def reconcile_usage_facts(service: UsageReconcileLedger) -> int:
    """恢复超时预留；确认漂移后按事实做版本化绝对覆盖，再复核聚合差异。"""

    recovered = await service.recover_orphans()
    drift = await service.measure_drift()
    if drift.mismatches:
        await service.rebuild(actor=RECONCILE_REBUILD_ACTOR)
        drift = await service.measure_drift()
    return recovered + drift.mismatches


def _version_key(dimension_key: str) -> str:
    return f"usage:projection:version:{dimension_key}"


def _ready_key(date_key: str) -> str:
    return f"usage:projection:ready:{date_key}"


async def _projection_rows(
    connection: AsyncConnection,
    keys: Sequence[str],
) -> tuple[ProjectionRow, ...]:
    if not keys:
        return ()
    result = await connection.execute(
        text(
            """
            SELECT dimension_key,kind,usage_date,value,version,expires_at
            FROM usage_projection
            WHERE dimension_key=ANY(CAST(:keys AS text[]))
            ORDER BY dimension_key
            """
        ),
        {"keys": list(dict.fromkeys(keys))},
    )
    return tuple(
        ProjectionRow(
            str(row["dimension_key"]),
            str(row["kind"]),
            row["usage_date"],
            int(row["value"]),
            int(row["version"]),
            row["expires_at"],
        )
        for row in result.mappings()
    )


async def _lock_projection_writer(connection: AsyncConnection) -> None:
    """共享事务锁封住分页重建的提交边界；写者之间并行，重建时失败关闭。"""

    locked = await connection.scalar(
        text("SELECT pg_try_advisory_xact_lock_shared(hashtextextended(:name, 0))"),
        {"name": "usage:projection:rebuild"},
    )
    if not locked:
        raise UsageProjectionUnavailable("usage projection rebuild in progress")


async def _lock_projection_keys(
    connection: AsyncConnection,
    keys: Sequence[str],
    *,
    namespace: int,
) -> None:
    unique_keys = sorted(set(keys))
    if not unique_keys:
        return
    await connection.execute(
        text(
            """
            SELECT pg_advisory_xact_lock(
              hashtextextended(locked.dimension_key,:namespace)
            )
            FROM (
              SELECT unnest(CAST(:keys AS text[])) dimension_key
              ORDER BY dimension_key
            ) locked
            """
        ),
        {"keys": unique_keys, "namespace": namespace},
    )


def _latest_projection_rows(rows: Sequence[ProjectionRow]) -> tuple[ProjectionRow, ...]:
    latest: dict[str, ProjectionRow] = {}
    for row in rows:
        current = latest.get(row.dimension_key)
        if current is None or row.version >= current.version:
            latest[row.dimension_key] = row
    return tuple(sorted(latest.values(), key=lambda item: item.dimension_key))


async def _database_now(connection: AsyncConnection) -> datetime:
    value = await connection.scalar(text("SELECT now()"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise UsageReservationConflict("database clock unavailable")
    return value


async def _change_projections(
    connection: AsyncConnection,
    changes: Sequence[_ProjectionChange],
) -> tuple[ProjectionRow, ...]:
    """批量更新绝对投影，缩短共享限额行的事务持锁时间。"""

    if not changes:
        return ()
    if len({change.dimension_key for change in changes}) != len(changes):
        raise ValueError("projection changes must have unique dimensions")
    await _lock_projection_writer(connection)
    payload = json.dumps(
        [
            {
                "dimension_key": change.dimension_key,
                "kind": change.kind,
                "usage_date": change.usage_date.isoformat(),
                "window_key": change.window_key,
                "delta": change.delta,
                "expires_at": change.expires_at.isoformat(),
                "reset_on_window_change": change.reset_on_window_change,
            }
            for change in changes
        ],
        separators=(",", ":"),
    )
    incoming_sql = """
        SELECT *
        FROM jsonb_to_recordset(CAST(:changes AS jsonb)) AS incoming(
          dimension_key text,kind text,usage_date date,window_key text,
          delta bigint,expires_at timestamptz,reset_on_window_change boolean
        )
    """
    if all(change.delta >= 0 and change.reset_on_window_change for change in changes):
        result = await connection.execute(
            text(
                f"""
                WITH incoming AS ({incoming_sql})
                INSERT INTO usage_projection(
                  dimension_key,kind,usage_date,window_key,value,version,expires_at
                )
                SELECT
                  incoming.dimension_key,incoming.kind,incoming.usage_date,
                  incoming.window_key,incoming.delta,
                  nextval('usage_projection_version_seq'),incoming.expires_at
                FROM incoming
                ON CONFLICT(dimension_key) DO UPDATE SET
                  value=CASE
                    WHEN usage_projection.window_key=EXCLUDED.window_key
                      THEN usage_projection.value+EXCLUDED.value
                    ELSE EXCLUDED.value
                  END,
                  usage_date=EXCLUDED.usage_date,
                  window_key=EXCLUDED.window_key,
                  version=nextval('usage_projection_version_seq'),
                  expires_at=CASE
                    WHEN usage_projection.window_key=EXCLUDED.window_key
                      THEN GREATEST(
                        usage_projection.expires_at,EXCLUDED.expires_at
                      )
                    ELSE EXCLUDED.expires_at
                  END,
                  updated_at=now()
                RETURNING dimension_key,kind,usage_date,value,version,expires_at
                """
            ),
            {"changes": payload},
        )
    elif all(change.delta <= 0 and not change.reset_on_window_change for change in changes):
        result = await connection.execute(
            text(
                f"""
                WITH incoming AS ({incoming_sql})
                UPDATE usage_projection projection SET
                  value=CASE
                    WHEN projection.window_key=incoming.window_key
                      THEN GREATEST(0,projection.value+incoming.delta)
                    ELSE projection.value
                  END,
                  version=nextval('usage_projection_version_seq'),
                  expires_at=CASE
                    WHEN projection.window_key=incoming.window_key
                      THEN GREATEST(projection.expires_at,incoming.expires_at)
                    ELSE projection.expires_at
                  END,
                  updated_at=now()
                FROM incoming
                WHERE projection.dimension_key=incoming.dimension_key
                RETURNING projection.dimension_key,projection.kind,
                  projection.usage_date,projection.value,projection.version,
                  projection.expires_at
                """
            ),
            {"changes": payload},
        )
    else:
        raise ValueError("projection batch changes must share one direction")
    rows = list(result.mappings())
    if len(rows) != len(changes):
        raise UsageReservationConflict("projection batch update conflict")
    return tuple(
        ProjectionRow(
            str(row["dimension_key"]),
            str(row["kind"]),
            row["usage_date"],
            int(row["value"]),
            int(row["version"]),
            row["expires_at"],
        )
        for row in sorted(rows, key=lambda item: str(item["dimension_key"]))
    )


class UsageProjectionMixin:
    """UsageLedgerService 的投影维护；PostgreSQL 事实不可确认时失败关闭。"""

    redis: UsageRedis
    settings: Settings
    pooled: bool
    clock: Any

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def _claim_rebuild_lock(self, connection: Any, date_key: str) -> str:
        """数据库会话锁决定 Owner；Redis 屏障覆盖跨日及普通投影写入。"""

        token = str(uuid4())
        try:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))"),
                {"name": "usage:projection:rebuild"},
            )
            if not locked:
                raise UsageProjectionUnavailable("usage projection rebuild in progress")
            await self.redis.eval(
                BEGIN_PROJECTION_REBUILD_LUA,
                2,
                PROJECTION_REBUILD_KEY,
                _ready_key(date_key),
                token,
                PROJECTION_REBUILD_TTL_S,
            )
        except BaseException as exc:
            await self._release_rebuild_lock(connection)
            if isinstance(exc, Exception) and not isinstance(exc, UsageProjectionUnavailable):
                raise UsageProjectionUnavailable("usage projection rebuild unavailable") from exc
            raise
        return token

    async def _renew_rebuild(self, token: str) -> None:
        renewed = await self.redis.eval(
            RENEW_PROJECTION_REBUILD_LUA,
            1,
            PROJECTION_REBUILD_KEY,
            token,
            PROJECTION_REBUILD_TTL_S,
        )
        if int(renewed) != 1:
            raise UsageProjectionUnavailable("usage projection rebuild owner lost")

    async def _publish_ready(self, dates: Mapping[date, datetime], *, token: str = "") -> None:
        ordered = sorted(dates.items())
        published = await self.redis.eval(
            PUBLISH_PROJECTION_READY_LUA,
            len(ordered) + 1,
            PROJECTION_REBUILD_KEY,
            *(_ready_key(day.strftime("%Y%m%d")) for day, _ in ordered),
            token,
            *(int(expires.timestamp() * 1000) for _, expires in ordered),
        )
        if int(published) != 1:
            raise UsageProjectionUnavailable("usage projection rebuild in progress")

    async def _release_rebuild_lock(self, connection: Any) -> None:
        # 分页 SQL 失败会中止事务；先 rollback 才能在池化会话上真正释放 session lock。
        await connection.rollback()
        await connection.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))"),
            {"name": "usage:projection:rebuild"},
        )

    async def _has_projection_facts(self, usage_date: date) -> bool:
        async with self._engine().connect() as connection:
            value = await connection.scalar(
                text(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM usage_projection
                      WHERE usage_date=:usage_date AND expires_at>now()
                    )
                    """
                ),
                {"usage_date": usage_date},
            )
            return bool(value)

    async def ensure_ready(self, now: datetime | None = None) -> None:
        """Redis flush 后若数据库仍有事实，禁止把缺失投影误当作零。"""

        current = now or self.clock()
        date_key, usage_date, next_day = shanghai_day(current)
        marker_key = _ready_key(date_key)
        try:
            rebuilding, marker = await self.redis.mget([PROJECTION_REBUILD_KEY, marker_key])
            if rebuilding is not None:
                raise UsageProjectionUnavailable("usage projection rebuild in progress")
        except UsageProjectionUnavailable:
            raise
        except Exception as exc:
            raise UsageProjectionUnavailable("usage projection redis unavailable") from exc
        if marker is not None:
            return
        if await self._has_projection_facts(usage_date):
            await self.rebuild(actor=RECONCILE_REBUILD_ACTOR)
            return
        try:
            await self._publish_ready({usage_date: next_day})
        except Exception as exc:
            raise UsageProjectionUnavailable("usage projection redis unavailable") from exc

    async def _apply_rows(self, rows: Sequence[ProjectionRow]) -> int:
        """分块应用版本化绝对值；普通写入不得把未完成的重建标为 ready。"""

        applied = 0
        try:
            for batch in _projection_batches(rows):
                keys: list[str] = []
                arguments: list[str] = []
                for row in sorted(batch, key=lambda item: item.dimension_key):
                    key, version_key, value, version, expires = _projection_arguments(row)
                    keys.extend((key, version_key))
                    arguments.extend((value, version, expires))
                applied += int(
                    await self.redis.eval(
                        APPLY_PROJECTIONS_LUA,
                        len(keys),
                        *keys,
                        *arguments,
                    )
                )
        except Exception as exc:
            raise UsageProjectionUnavailable("usage projection write unavailable") from exc
        return applied

    async def _projection_pages(self, connection: Any) -> AsyncIterator[list[ProjectionRow]]:
        """稳定维度游标分页，数据库与 Python 每次最多保留一页。"""

        cursor = ""
        while True:
            result = await connection.execute(
                text("""
                    SELECT dimension_key,kind,usage_date,value,version,expires_at
                    FROM usage_projection
                    WHERE expires_at>now() AND dimension_key>:cursor
                    ORDER BY dimension_key LIMIT :limit
                """),
                {"cursor": cursor, "limit": PROJECTION_BATCH_ROWS},
            )
            rows = [
                ProjectionRow(
                    str(row["dimension_key"]),
                    str(row["kind"]),
                    row["usage_date"],
                    int(row["value"]),
                    int(row["version"]),
                    row["expires_at"],
                )
                for row in result.mappings()
            ]
            if not rows:
                break
            yield rows
            cursor = rows[-1].dimension_key
            if len(rows) < PROJECTION_BATCH_ROWS:
                break

    async def _mark_uncertain(self, reservation_id: UUID, error_type: str) -> None:
        safe_error = re.sub(r"[^A-Za-z0-9_.]", "", error_type)[:64] or "ProjectionError"
        async with self._engine().begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE usage_reservation SET
                      state='uncertain',last_error=:error,updated_at=now()
                    WHERE id=:reservation_id AND state='reserved'
                    """
                ),
                {"reservation_id": reservation_id, "error": safe_error},
            )

    async def rebuild(self, *, actor: str = "system:usage-projection") -> int:
        """分页重建，全部投影与审计成功后才原子开放 ready。"""

        engine = self._engine()
        date_key, usage_date, next_day = shanghai_day(self.clock())
        counts = {"quota": 0, "frequency": 0}
        dimension_count = 0
        dates: dict[date, datetime] = {usage_date: next_day}
        async with engine.connect() as owner:
            token = await self._claim_rebuild_lock(owner, date_key)
            try:
                async for rows in self._projection_pages(owner):
                    await self._renew_rebuild(token)
                    await self._apply_rows(rows)
                    dimension_count += len(rows)
                    for row in rows:
                        counts[row.kind] += 1
                        dates[row.usage_date] = max(
                            dates.get(row.usage_date, row.expires_at), row.expires_at
                        )
                async with engine.begin() as connection:
                    await bind_connection_system_audit(
                        connection,
                        actor_name=actor,
                        action="usage_projection_rebuild",
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(
                              actor,actor_subject_kind,role,action,object_type,object_id,
                              after_val
                            ) VALUES(
                              :actor,'system','system','usage_projection_rebuild',
                              'usage_projection','all',
                              jsonb_build_object(
                                'dimension_count',CAST(:dimension_count AS integer),
                                'quota_dimensions',CAST(:quota_dimensions AS integer),
                                'frequency_dimensions',CAST(:frequency_dimensions AS integer)
                              )
                            )
                            """
                        ),
                        {
                            "actor": actor,
                            "dimension_count": dimension_count,
                            "quota_dimensions": counts["quota"],
                            "frequency_dimensions": counts["frequency"],
                        },
                    )
                await self._publish_ready(dates, token=token)
            except UsageProjectionUnavailable:
                raise
            except Exception as exc:
                raise UsageProjectionUnavailable("usage projection rebuild unavailable") from exc
            finally:
                await self._release_rebuild_lock(owner)
        return dimension_count

    async def measure_drift(self) -> UsageDrift:
        """聚合 Redis/事实投影差异；不持久化或返回任何号码索引。"""

        engine = self._engine()
        aggregates = {"quota": [0, 0], "frequency": [0, 0]}
        async with engine.connect() as connection:
            async for rows in self._projection_pages(connection):
                for batch in _projection_batches(rows):
                    # 不可读仍是 UNKNOWN；不得写入伪造的零漂移快照。
                    raw_values = await self.redis.mget([row.dimension_key for row in batch])
                    for row, raw in zip(batch, raw_values, strict=True):
                        try:
                            actual = int(raw) if raw is not None else 0
                        except (TypeError, ValueError):
                            actual = 0
                        if actual != row.value:
                            aggregates[row.kind][0] += 1
                            aggregates[row.kind][1] += abs(row.value - actual)
        drift = UsageDrift(
            aggregates["quota"][0],
            aggregates["quota"][1],
            aggregates["frequency"][0],
            aggregates["frequency"][1],
        )
        async with engine.begin() as connection:
            for kind, values in aggregates.items():
                await connection.execute(
                    text(
                        """
                        INSERT INTO usage_projection_drift(
                          kind,mismatched_dimensions,absolute_delta,checked_at
                        ) VALUES(:kind,:mismatches,:delta,now())
                        ON CONFLICT(kind) DO UPDATE SET
                          mismatched_dimensions=EXCLUDED.mismatched_dimensions,
                          absolute_delta=EXCLUDED.absolute_delta,
                          checked_at=EXCLUDED.checked_at
                        """
                    ),
                    {
                        "kind": kind,
                        "mismatches": values[0],
                        "delta": values[1],
                    },
                )
            if drift.mismatches:
                await connection.execute(
                    text(
                        """
                        INSERT INTO alert_log(
                          alert_type,level,title,detail,channels,dedup_key
                        )
                        SELECT
                          'usage_projection_drift','crit',
                          '配额或频控投影与事实账本不一致',
                          CAST(:detail AS jsonb),'log-sink',
                          'usage_projection_drift'
                        WHERE NOT EXISTS (
                          SELECT 1 FROM alert_log
                          WHERE dedup_key='usage_projection_drift'
                            AND created_at>=now()-interval '4 hours'
                        )
                        """
                    ),
                    {
                        "detail": json.dumps(
                            {
                                "quota_mismatches": drift.quota_mismatches,
                                "quota_absolute_delta": drift.quota_delta,
                                "frequency_mismatches": drift.frequency_mismatches,
                                "frequency_absolute_delta": drift.frequency_delta,
                                "action": "巡检将按事实覆盖 Redis 投影后复核",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    },
                )
        return drift
