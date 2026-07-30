"""配额与号码频控的 PostgreSQL 事实账本及可重建 Redis 投影。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import database_engine
from app.services.freq import FrequencyLimits
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.quota import QuotaExceeded
from app.settings import Settings, get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
HMAC_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DATE_KEY_PATTERN = re.compile(r"^[0-9]{8}$")
UUID_FRAGMENT = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
REQUEST_KEY_PATTERN = re.compile(
    rf"^(?:"
    rf"acceptance:(?:[0-9]+:[0-9a-f]{{64}}:[0-9]{{8}}|{UUID_FRAGMENT})"
    rf"|legacy:batch:[0-9a-f]{{32}}"
    rf")$"
)
RELEASE_EVENT_ID_PATTERN = re.compile(
    rf"^(?:"
    rf"batch:[0-9a-f]{{32}}:cancelled"
    rf"|approval:[1-9][0-9]*:(?:rejected|expired)"
    rf"|usage:{UUID_FRAGMENT}:"
    rf"(?:acceptance-failed|all-filtered|idempotent-reuse|orphan-recovery)"
    rf")$"
)

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


class UsageRedis(Protocol):
    async def eval(self, *args: Any) -> Any: ...

    async def get(self, name: str) -> Any: ...

    async def set(self, name: str, value: Any, **kwargs: Any) -> Any: ...

    async def mget(self, keys: Sequence[str]) -> list[Any]: ...


class UsageProjectionUnavailable(RuntimeError):
    """Redis 投影缺失或不可确认，受理必须失败关闭。"""


class UsageReservationConflict(RuntimeError):
    """同一稳定请求引用出现不一致的账本合同。"""


@dataclass(frozen=True, slots=True)
class UsageReservation:
    reservation_id: UUID
    reused: bool = False


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    dimension_key: str
    kind: str
    usage_date: date
    value: int
    version: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _ProjectionChange:
    dimension_key: str
    kind: str
    usage_date: date
    window_key: str
    delta: int
    expires_at: datetime
    reset_on_window_change: bool = True


@dataclass(frozen=True, slots=True)
class UsageDrift:
    quota_mismatches: int
    quota_delta: int
    frequency_mismatches: int
    frequency_delta: int

    @property
    def mismatches(self) -> int:
        return self.quota_mismatches + self.frequency_mismatches


def utc_now() -> datetime:
    return datetime.now(UTC)


def shanghai_day(now: datetime) -> tuple[str, date, datetime]:
    """返回上海自然日键、日期和下一日边界。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("usage ledger clock must be timezone-aware")
    local = now.astimezone(SHANGHAI)
    next_day = (local + timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return local.strftime("%Y%m%d"), local.date(), next_day


def frequency_windows(now: datetime) -> tuple[str, datetime, str, date, datetime]:
    """返回 UTC 分钟窗与上海自然日窗。"""

    date_key, usage_date, next_day = shanghai_day(now)
    next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    minute_window = str(int(now.timestamp() // 60))
    return minute_window, next_minute, date_key, usage_date, next_day


def _version_key(dimension_key: str) -> str:
    return f"usage:projection:version:{dimension_key}"


def _ready_key(date_key: str) -> str:
    return f"usage:projection:ready:{date_key}"


def _safe_request_key(value: str) -> str:
    if REQUEST_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid usage reservation request key")
    return value


def _safe_event_id(value: str) -> str:
    if RELEASE_EVENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid usage release event id")
    return value


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


async def _change_projections(
    connection: AsyncConnection,
    changes: Sequence[_ProjectionChange],
) -> tuple[ProjectionRow, ...]:
    """批量更新绝对投影，缩短共享限额行的事务持锁时间。"""

    if not changes:
        return ()
    if len({change.dimension_key for change in changes}) != len(changes):
        raise ValueError("projection changes must have unique dimensions")
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


async def commit_usage_reservation(
    connection: AsyncConnection,
    *,
    reservation_id: UUID,
    batch_id: int,
) -> None:
    """在批次事务内把预留转为 committed；批次外键已在同一事务写入。"""

    result = await connection.execute(
        text(
            """
            UPDATE usage_reservation SET
              state='committed',committed_at=now(),updated_at=now()
            WHERE id=:reservation_id AND state='reserved'
              AND EXISTS (
                SELECT 1 FROM sms_batch b
                WHERE b.id=:batch_id
                  AND b.usage_reservation_id=usage_reservation.id
              )
            """
        ),
        {"reservation_id": reservation_id, "batch_id": batch_id},
    )
    if result.rowcount != 1:
        current = await connection.execute(
            text(
                """
                SELECT r.state,b.id batch_id FROM usage_reservation r
                LEFT JOIN sms_batch b ON b.usage_reservation_id=r.id
                WHERE r.id=:reservation_id FOR UPDATE OF r
                """
            ),
            {"reservation_id": reservation_id},
        )
        row = current.mappings().one_or_none()
        if row is None or str(row["state"]) != "committed" or int(row["batch_id"] or 0) != batch_id:
            raise UsageReservationConflict("usage reservation commit conflict")


async def _request_release(
    connection: AsyncConnection,
    *,
    reservation_id: UUID,
    event_id: str,
) -> tuple[bool, tuple[ProjectionRow, ...]]:
    """在调用方事务内建立唯一释放事实并扣减数据库权威投影。"""

    event_id = _safe_event_id(event_id)
    selected = await connection.execute(
        text(
            """
            SELECT state,release_event_id FROM usage_reservation
            WHERE id=:reservation_id FOR UPDATE
            """
        ),
        {"reservation_id": reservation_id},
    )
    row = selected.mappings().one_or_none()
    if row is None:
        raise UsageReservationConflict("usage reservation unavailable")
    state = str(row["state"])
    persisted_event = str(row["release_event_id"]) if row["release_event_id"] is not None else None
    changed = False
    rows: tuple[ProjectionRow, ...] = ()
    if state in {"reserved", "committed", "uncertain"}:
        if persisted_event is not None and persisted_event != event_id:
            raise UsageReservationConflict("usage release event changed")
        await connection.execute(
            text(
                """
                UPDATE usage_reservation SET
                  state='release_requested',release_event_id=:event_id,
                  release_requested_at=now(),updated_at=now()
                WHERE id=:reservation_id
                """
            ),
            {"reservation_id": reservation_id, "event_id": event_id},
        )
        entries = await connection.execute(
            text(
                """
                SELECT projection_key,kind,usage_date,window_key,
                       sum(amount)::bigint amount,
                       max(expires_at) expires_at
                FROM (
                  SELECT projection_key,'quota'::text kind,usage_date,
                         to_char(usage_date,'YYYYMMDD') window_key,
                         amount,expires_at
                  FROM usage_quota_entry WHERE reservation_id=:reservation_id
                  UNION ALL
                  SELECT projection_key,'frequency'::text kind,usage_date,
                         window_key,
                         CASE WHEN counted THEN 1 ELSE 0 END amount,expires_at
                  FROM usage_frequency_entry WHERE reservation_id=:reservation_id
                ) facts
                GROUP BY projection_key,kind,usage_date,window_key
                """
            ),
            {"reservation_id": reservation_id},
        )
        changes: list[_ProjectionChange] = []
        for entry in entries.mappings():
            amount = int(entry["amount"])
            if amount <= 0:
                continue
            changes.append(
                _ProjectionChange(
                    dimension_key=str(entry["projection_key"]),
                    kind=str(entry["kind"]),
                    usage_date=entry["usage_date"],
                    window_key=str(entry["window_key"]),
                    delta=-amount,
                    expires_at=entry["expires_at"],
                    reset_on_window_change=False,
                )
            )
        await _lock_projection_keys(
            connection,
            [change.dimension_key for change in changes if change.kind == "frequency"],
            namespace=43,
        )
        await _lock_projection_keys(
            connection,
            [change.dimension_key for change in changes if change.kind == "quota"],
            namespace=47,
        )
        rows = await _change_projections(connection, changes)
        changed = True
    elif state == "release_requested":
        if persisted_event != event_id:
            raise UsageReservationConflict("usage release event changed")
        key_result = await connection.execute(
            text(
                """
                SELECT projection_key FROM usage_quota_entry
                WHERE reservation_id=:reservation_id
                UNION
                SELECT projection_key FROM usage_frequency_entry
                WHERE reservation_id=:reservation_id AND counted
                """
            ),
            {"reservation_id": reservation_id},
        )
        rows = await _projection_rows(
            connection,
            [str(value) for value in key_result.scalars()],
        )
    elif state == "released":
        if persisted_event is not None and persisted_event != event_id:
            raise UsageReservationConflict("usage release event changed")
        return False, ()
    else:
        raise UsageReservationConflict("invalid usage reservation state")

    await enqueue_outbox(
        connection,
        OutboxEventSpec(
            event_type="usage.release",
            aggregate_type="usage_reservation",
            aggregate_id=str(reservation_id),
            task_name="app.tasks.outbox.release_usage",
            queue="realtime",
            args=(str(reservation_id),),
            dedup_key=f"usage.release:{reservation_id}",
        ),
    )
    return changed, rows


async def request_usage_release_for_batch(
    connection: AsyncConnection,
    *,
    batch_id: int,
    event_id: str,
) -> bool:
    """终态业务事务内按 batch 稳定引用建立释放事实。"""

    result = await connection.execute(
        text(
            """
            SELECT usage_reservation_id FROM sms_batch
            WHERE id=:batch_id FOR UPDATE
            """
        ),
        {"batch_id": batch_id},
    )
    reservation_id = result.scalar_one_or_none()
    if reservation_id is None:
        return False
    await _request_release(
        connection,
        reservation_id=UUID(str(reservation_id)),
        event_id=event_id,
    )
    return True


class UsageLedgerService:
    """以 PostgreSQL 串行化限额决策，并把绝对值安全投影到 Redis。"""

    def __init__(
        self,
        redis: UsageRedis,
        settings: Settings | None = None,
        *,
        pooled: bool = True,
        clock: Any = utc_now,
    ) -> None:
        self.redis = redis
        self.settings = settings or get_settings()
        self.pooled = pooled
        self.clock = clock

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

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
            marker = await self.redis.get(marker_key)
        except Exception as exc:
            raise UsageProjectionUnavailable("usage projection redis unavailable") from exc
        if marker is not None:
            return
        if await self._has_projection_facts(usage_date):
            try:
                owns_rebuild = await self.redis.set(
                    f"usage:projection:rebuild:{date_key}",
                    str(uuid4()),
                    nx=True,
                    ex=60,
                )
            except Exception as exc:
                raise UsageProjectionUnavailable("usage projection redis unavailable") from exc
            if not owns_rebuild:
                raise UsageProjectionUnavailable("usage projection rebuild in progress")
            await self.rebuild(actor="system:usage-projection-auto")
            return
        try:
            await self.redis.set(
                marker_key,
                "1",
                nx=True,
                pxat=int(next_day.timestamp() * 1000),
            )
        except Exception as exc:
            raise UsageProjectionUnavailable("usage projection redis unavailable") from exc

    async def _apply_rows(self, rows: Sequence[ProjectionRow]) -> int:
        applied = 0
        dates: dict[date, datetime] = {}
        try:
            ordered = sorted(rows, key=lambda row: row.dimension_key)
            if ordered:
                keys: list[str] = []
                arguments: list[str] = []
                for row in ordered:
                    expire_ms = int(row.expires_at.timestamp() * 1000)
                    keys.extend((row.dimension_key, _version_key(row.dimension_key)))
                    arguments.extend((str(row.value), str(row.version), str(expire_ms)))
                    dates[row.usage_date] = max(
                        dates.get(row.usage_date, row.expires_at),
                        row.expires_at,
                    )
                value = await self.redis.eval(
                    APPLY_PROJECTIONS_LUA,
                    len(keys),
                    *keys,
                    *arguments,
                )
                applied += int(value)
            for usage_date, expires_at in dates.items():
                await self.redis.set(
                    _ready_key(usage_date.strftime("%Y%m%d")),
                    "1",
                    pxat=int(expires_at.timestamp() * 1000),
                )
        except Exception as exc:
            raise UsageProjectionUnavailable("usage projection write unavailable") from exc
        return applied

    async def start_reservation(
        self,
        *,
        request_key: str,
        app_id: int,
        dept: str,
        category: str,
        now: datetime | None = None,
    ) -> UsageReservation:
        current = now or self.clock()
        await self.ensure_ready(current)
        request_key = _safe_request_key(request_key)
        _, usage_date, _ = shanghai_day(current)
        if app_id < 0 or not dept or category not in {"verify", "notice", "market"}:
            raise ValueError("invalid usage reservation")
        engine = self._engine()
        reservation_id = uuid4()
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO usage_reservation(
                      id,request_key,app_id,dept,category,usage_date,state
                    ) VALUES(
                      :id,:request_key,:app_id,:dept,:category,:usage_date,'reserved'
                    )
                    ON CONFLICT(request_key) WHERE state<>'released'
                    DO UPDATE SET request_key=EXCLUDED.request_key
                    RETURNING id,app_id,dept,category,usage_date
                    """
                ),
                {
                    "id": reservation_id,
                    "request_key": request_key,
                    "app_id": app_id,
                    "dept": dept,
                    "category": category,
                    "usage_date": usage_date,
                },
            )
            row = result.mappings().one()
            expected = (app_id, dept, category, usage_date)
            persisted = (
                int(row["app_id"]),
                str(row["dept"]),
                str(row["category"]),
                row["usage_date"],
            )
            if expected != persisted:
                raise UsageReservationConflict("usage reservation contract changed")
            persisted_id = UUID(str(row["id"]))
            return UsageReservation(
                persisted_id,
                reused=persisted_id != reservation_id,
            )

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

    async def allow_frequency(
        self,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        phone_hmac: str,
        hmac_aliases: Mapping[int, str],
        limits: FrequencyLimits,
        now: datetime | None = None,
    ) -> bool:
        """对一个不可逆 HMAC 主体做原子频控决策；过滤项不进入计数投影。"""

        if category == "notice":
            return True
        if category not in {"verify", "market"}:
            raise ValueError("unsupported frequency category")
        if HMAC_PATTERN.fullmatch(phone_hmac) is None:
            raise ValueError("phone_hmac must be 64 lowercase hex characters")
        if (
            not hmac_aliases
            or any(
                version < 1 or HMAC_PATTERN.fullmatch(digest) is None
                for version, digest in hmac_aliases.items()
            )
            or phone_hmac not in hmac_aliases.values()
        ):
            raise ValueError("invalid frequency hmac aliases")
        current = now or self.clock()
        await self.ensure_ready(current)
        minute_window, minute_expires, day_window, usage_date, day_expires = frequency_windows(
            current
        )
        engine = self._engine()
        async with engine.begin() as connection:
            await _lock_projection_keys(
                connection,
                list(hmac_aliases.values()),
                namespace=41,
            )
            reservation = await connection.execute(
                text(
                    """
                    SELECT state,app_id,category,usage_date
                    FROM usage_reservation WHERE id=:id FOR UPDATE
                    """
                ),
                {"id": reservation_id},
            )
            reservation_row = reservation.mappings().one_or_none()
            if reservation_row is None or str(reservation_row["state"]) != "reserved":
                raise UsageReservationConflict("usage reservation is not writable")
            if (
                int(reservation_row["app_id"]) != app_id
                or str(reservation_row["category"]) != category
                or reservation_row["usage_date"] != usage_date
            ):
                raise UsageReservationConflict("frequency reservation contract changed")

            alias_result = await connection.execute(
                text(
                    """
                    SELECT DISTINCT subject_id FROM usage_frequency_alias
                    WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": list(hmac_aliases.values())},
            )
            subject_ids = {UUID(str(value)) for value in alias_result.scalars()}
            if subject_ids:
                preferred_subject = await connection.scalar(
                    text(
                        """
                        SELECT subject_id FROM usage_frequency_alias
                        WHERE phone_hmac=:preferred_digest
                        """
                    ),
                    {"preferred_digest": hmac_aliases[min(hmac_aliases)]},
                )
                subject_result = await connection.execute(
                    text(
                        """
                        SELECT id,projection_hmac FROM usage_frequency_subject
                        WHERE id=ANY(CAST(:subject_ids AS uuid[]))
                        ORDER BY projection_hmac,id
                        """
                    ),
                    {"subject_ids": list(subject_ids)},
                )
                subject_rows = list(subject_result.mappings())
                if len(subject_rows) != len(subject_ids):
                    raise UsageReservationConflict("frequency subject unavailable")
                preferred_subject_id = (
                    UUID(str(preferred_subject)) if preferred_subject is not None else None
                )
                canonical = next(
                    (row for row in subject_rows if UUID(str(row["id"])) == preferred_subject_id),
                    subject_rows[0],
                )
                subject_id = UUID(str(canonical["id"]))
                projection_hmac = str(canonical["projection_hmac"])
                for source in subject_rows:
                    source_id = UUID(str(source["id"]))
                    if source_id == subject_id:
                        continue
                    await connection.execute(
                        text(
                            """
                            UPDATE usage_frequency_alias SET subject_id=:target_id
                            WHERE subject_id=:source_id
                            """
                        ),
                        {"target_id": subject_id, "source_id": source_id},
                    )
                    await connection.execute(
                        text(
                            """
                            UPDATE usage_frequency_entry SET subject_id=:target_id
                            WHERE subject_id=:source_id
                            """
                        ),
                        {"target_id": subject_id, "source_id": source_id},
                    )
                    await connection.execute(
                        text("DELETE FROM usage_frequency_subject WHERE id=:source_id"),
                        {"source_id": source_id},
                    )
            else:
                subject_id = uuid4()
                projection_hmac = hmac_aliases[min(hmac_aliases)]
                await connection.execute(
                    text(
                        """
                        INSERT INTO usage_frequency_subject(id,projection_hmac)
                        VALUES(:id,:projection_hmac)
                        """
                    ),
                    {"id": subject_id, "projection_hmac": projection_hmac},
                )
            alias_payload = json.dumps(
                [
                    {"key_version": version, "phone_hmac": digest}
                    for version, digest in sorted(hmac_aliases.items())
                ],
                separators=(",", ":"),
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    )
                    SELECT
                      :subject_id,alias.key_version,alias.phone_hmac
                    FROM jsonb_to_recordset(CAST(:aliases AS jsonb)) AS alias(
                      key_version smallint,phone_hmac char(64)
                    )
                    ON CONFLICT(phone_hmac) DO NOTHING
                    """
                ),
                {"subject_id": subject_id, "aliases": alias_payload},
            )
            alias_check = await connection.execute(
                text(
                    """
                    SELECT count(DISTINCT subject_id) FROM usage_frequency_alias
                    WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": list(hmac_aliases.values())},
            )
            if int(alias_check.scalar_one()) != 1:
                raise UsageReservationConflict("frequency alias write conflict")

            expected_windows = ("minute", "day") if category == "verify" else ("day",)
            existing = await connection.execute(
                text(
                    """
                    SELECT window_kind,counted FROM usage_frequency_entry
                    WHERE reservation_id=:reservation_id AND subject_id=:subject_id
                    """
                ),
                {"reservation_id": reservation_id, "subject_id": subject_id},
            )
            existing_rows = {
                str(row["window_kind"]): bool(row["counted"]) for row in existing.mappings()
            }
            if set(existing_rows) == set(expected_windows):
                keys_result = await connection.execute(
                    text(
                        """
                        SELECT projection_key FROM usage_frequency_entry
                        WHERE reservation_id=:reservation_id
                          AND subject_id=:subject_id AND counted
                        """
                    ),
                    {"reservation_id": reservation_id, "subject_id": subject_id},
                )
                rows = await _projection_rows(
                    connection,
                    [str(value) for value in keys_result.scalars()],
                )
                allowed = all(existing_rows.values())
            elif existing_rows:
                raise UsageReservationConflict("partial frequency decision persisted")
            else:
                specs: tuple[tuple[str, str, str, int, datetime], ...]
                if category == "verify":
                    specs = (
                        (
                            "minute",
                            minute_window,
                            f"freq:v:{projection_hmac}:m",
                            limits.verify_per_minute,
                            minute_expires,
                        ),
                        (
                            "day",
                            day_window,
                            f"freq:v:{projection_hmac}:d",
                            limits.verify_per_day,
                            day_expires,
                        ),
                    )
                else:
                    specs = (
                        (
                            "day",
                            day_window,
                            f"freq:m:{app_id}:{projection_hmac}:d",
                            limits.market_per_day,
                            day_expires,
                        ),
                    )
                await _lock_projection_keys(
                    connection,
                    [key for _, _, key, _, _ in specs],
                    namespace=43,
                )
                spec_payload = json.dumps(
                    [
                        {
                            "window_kind": window_kind,
                            "window_key": window_key,
                            "projection_key": key,
                            "expires_at": expires_at.isoformat(),
                        }
                        for window_kind, window_key, key, _, expires_at in specs
                    ],
                    separators=(",", ":"),
                )
                count_result = await connection.execute(
                    text(
                        """
                        WITH expected AS (
                          SELECT *
                          FROM jsonb_to_recordset(CAST(:specs AS jsonb)) AS item(
                            window_kind text,window_key text,
                            projection_key text,expires_at timestamptz
                          )
                        )
                        SELECT
                          expected.projection_key,
                          count(e.reservation_id)::bigint value
                        FROM expected
                        LEFT JOIN (
                          usage_frequency_entry e
                          JOIN usage_reservation r
                            ON r.id=e.reservation_id
                           AND r.state IN ('reserved','committed','uncertain')
                        ) ON e.subject_id=:subject_id
                          AND e.category=:category
                          AND e.app_id IS NOT DISTINCT FROM :frequency_app_id
                          AND e.window_kind=expected.window_kind
                          AND e.window_key=expected.window_key
                          AND e.counted
                        GROUP BY expected.projection_key
                        """
                    ),
                    {
                        "specs": spec_payload,
                        "subject_id": subject_id,
                        "category": category,
                        "frequency_app_id": app_id if category == "market" else None,
                    },
                )
                counts = {
                    str(item["projection_key"]): int(item["value"])
                    for item in count_result.mappings()
                }
                allowed = all(counts[key] + 1 <= limit for _, _, key, limit, _ in specs)
                await connection.execute(
                    text(
                        """
                        INSERT INTO usage_frequency_entry(
                          reservation_id,subject_id,app_id,category,
                          window_kind,window_key,usage_date,projection_key,
                          counted,expires_at
                        )
                        SELECT
                          :reservation_id,:subject_id,:app_id,:category,
                          item.window_kind,item.window_key,:usage_date,
                          item.projection_key,:counted,item.expires_at
                        FROM jsonb_to_recordset(CAST(:specs AS jsonb)) AS item(
                          window_kind text,window_key text,
                          projection_key text,expires_at timestamptz
                        )
                        """
                    ),
                    {
                        "reservation_id": reservation_id,
                        "subject_id": subject_id,
                        "app_id": app_id if category == "market" else None,
                        "category": category,
                        "usage_date": usage_date,
                        "counted": allowed,
                        "specs": spec_payload,
                    },
                )
                rows = (
                    await _change_projections(
                        connection,
                        [
                            _ProjectionChange(
                                dimension_key=key,
                                kind="frequency",
                                usage_date=usage_date,
                                window_key=window_key,
                                delta=1,
                                expires_at=expires_at,
                            )
                            for _, window_key, key, _, expires_at in specs
                        ],
                    )
                    if allowed
                    else ()
                )
        try:
            await self._apply_rows(rows)
        except UsageProjectionUnavailable as exc:
            await self._mark_uncertain(reservation_id, type(exc).__name__)
            raise
        return allowed

    async def reserve_quota(
        self,
        reservation_id: UUID,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        app_limit: int,
        dept_limit: int,
        expires_at: datetime,
    ) -> None:
        """在事实库内同时判定应用和部门配额，并生成三个解释维度。"""

        if (
            not DATE_KEY_PATTERN.fullmatch(date_key)
            or cost < 0
            or app_limit < 0
            or dept_limit < 0
            or category not in {"verify", "notice", "market"}
        ):
            raise ValueError("invalid quota reservation")
        usage_date = datetime.strptime(date_key, "%Y%m%d").date()
        await self.ensure_ready(self.clock())
        specs = (
            ("app", str(app_id), f"quota:app:{app_id}:{date_key}", app_limit),
            ("dept", dept, f"quota:dept:{dept}:{date_key}", dept_limit),
            (
                "volume",
                f"{app_id}:{category}",
                f"quota:volume:app:{app_id}:{category}:{date_key}",
                0,
            ),
        )
        engine = self._engine()
        async with engine.begin() as connection:
            reservation_result = await connection.execute(
                text(
                    """
                    SELECT state,app_id,dept,category,usage_date,quota_cost,
                           app_limit,dept_limit
                    FROM usage_reservation WHERE id=:id FOR UPDATE
                    """
                ),
                {"id": reservation_id},
            )
            row = reservation_result.mappings().one_or_none()
            if row is None or str(row["state"]) != "reserved":
                raise UsageReservationConflict("usage reservation is not writable")
            if (
                int(row["app_id"]) != app_id
                or str(row["dept"]) != dept
                or str(row["category"]) != category
                or row["usage_date"] != usage_date
            ):
                raise UsageReservationConflict("quota reservation contract changed")
            existing_count = int(
                await connection.scalar(
                    text(
                        """
                        SELECT count(*) FROM usage_quota_entry
                        WHERE reservation_id=:reservation_id
                        """
                    ),
                    {"reservation_id": reservation_id},
                )
                or 0
            )
            if existing_count:
                if (
                    existing_count != 3
                    or int(row["quota_cost"]) != cost
                    or int(row["app_limit"]) != app_limit
                    or int(row["dept_limit"]) != dept_limit
                ):
                    raise UsageReservationConflict("quota reservation contract changed")
                keys_result = await connection.execute(
                    text(
                        """
                        SELECT projection_key FROM usage_quota_entry
                        WHERE reservation_id=:reservation_id
                        """
                    ),
                    {"reservation_id": reservation_id},
                )
                rows = await _projection_rows(
                    connection,
                    [str(value) for value in keys_result.scalars()],
                )
            else:
                await _lock_projection_keys(
                    connection,
                    [key for _, _, key, _ in specs],
                    namespace=47,
                )
                current_result = await connection.execute(
                    text(
                        """
                        SELECT dimension_key,value FROM usage_projection
                        WHERE dimension_key=ANY(CAST(:keys AS text[]))
                        """
                    ),
                    {"keys": [key for _, _, key, _ in specs]},
                )
                current = {
                    str(item["dimension_key"]): int(item["value"])
                    for item in current_result.mappings()
                }
                app_key = specs[0][2]
                dept_key = specs[1][2]
                if (app_limit > 0 and current.get(app_key, 0) + cost > app_limit) or (
                    dept_limit > 0 and current.get(dept_key, 0) + cost > dept_limit
                ):
                    raise QuotaExceeded("日配额不足")
                await connection.execute(
                    text(
                        """
                        UPDATE usage_reservation SET
                          quota_cost=:cost,app_limit=:app_limit,dept_limit=:dept_limit,
                          updated_at=now()
                        WHERE id=:reservation_id
                        """
                    ),
                    {
                        "reservation_id": reservation_id,
                        "cost": cost,
                        "app_limit": app_limit,
                        "dept_limit": dept_limit,
                    },
                )
                entry_payload = json.dumps(
                    [
                        {
                            "dimension_kind": kind,
                            "dimension_value": dimension_value,
                            "projection_key": key,
                        }
                        for kind, dimension_value, key, _ in specs
                    ],
                    separators=(",", ":"),
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO usage_quota_entry(
                          reservation_id,dimension_kind,dimension_value,
                          usage_date,amount,projection_key,expires_at
                        )
                        SELECT
                          :reservation_id,entry.dimension_kind,
                          entry.dimension_value,:usage_date,:amount,
                          entry.projection_key,:expires_at
                        FROM jsonb_to_recordset(CAST(:entries AS jsonb)) AS entry(
                          dimension_kind text,dimension_value text,
                          projection_key text
                        )
                        """
                    ),
                    {
                        "reservation_id": reservation_id,
                        "usage_date": usage_date,
                        "amount": cost,
                        "expires_at": expires_at,
                        "entries": entry_payload,
                    },
                )
                rows = await _change_projections(
                    connection,
                    [
                        _ProjectionChange(
                            dimension_key=key,
                            kind="quota",
                            usage_date=usage_date,
                            window_key=date_key,
                            delta=cost,
                            expires_at=expires_at,
                        )
                        for _, _, key, _ in specs
                    ],
                )
        try:
            await self._apply_rows(rows)
        except UsageProjectionUnavailable as exc:
            await self._mark_uncertain(reservation_id, type(exc).__name__)
            raise

    async def request_release(self, reservation_id: UUID, *, event_id: str) -> bool:
        engine = self._engine()
        async with engine.begin() as connection:
            changed, _ = await _request_release(
                connection,
                reservation_id=reservation_id,
                event_id=event_id,
            )
            return changed

    async def apply_release(self, reservation_id: UUID) -> int:
        """Outbox effect：覆盖全部受影响绝对投影，成功后才标记 released。"""

        engine = self._engine()
        async with engine.connect() as connection:
            selected = await connection.execute(
                text(
                    """
                    SELECT state FROM usage_reservation
                    WHERE id=:reservation_id
                    """
                ),
                {"reservation_id": reservation_id},
            )
            state = selected.scalar_one_or_none()
            if state is None:
                raise UsageReservationConflict("usage reservation unavailable")
            key_result = await connection.execute(
                text(
                    """
                    SELECT projection_key FROM usage_quota_entry
                    WHERE reservation_id=:reservation_id
                    UNION
                    SELECT projection_key FROM usage_frequency_entry
                    WHERE reservation_id=:reservation_id AND counted
                    """
                ),
                {"reservation_id": reservation_id},
            )
            rows = await _projection_rows(
                connection,
                [str(value) for value in key_result.scalars()],
            )
        await self._apply_rows(rows)
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE usage_reservation SET
                      state='released',released_at=COALESCE(released_at,now()),
                      last_error=NULL,updated_at=now()
                    WHERE id=:reservation_id AND state='release_requested'
                    """
                ),
                {"reservation_id": reservation_id},
            )
            if result.rowcount == 0:
                current = await connection.scalar(
                    text("SELECT state FROM usage_reservation WHERE id=:reservation_id"),
                    {"reservation_id": reservation_id},
                )
                if current != "released":
                    raise UsageReservationConflict("usage release state conflict")
                return 0
        return 1

    async def rebuild(self, *, actor: str = "system:usage-projection") -> int:
        """从事实投影表安全重建 Redis；审计只记录维度数量。"""

        engine = self._engine()
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT dimension_key,kind,usage_date,value,version,expires_at
                    FROM usage_projection WHERE expires_at>now()
                    ORDER BY dimension_key
                    """
                )
            )
            rows = tuple(
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
        await self._apply_rows(rows)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO audit_log(
                      actor,action,object_type,object_id,after_val
                    ) VALUES(
                      :actor,'usage_projection_rebuild','usage_projection','all',
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
                    "dimension_count": len(rows),
                    "quota_dimensions": sum(row.kind == "quota" for row in rows),
                    "frequency_dimensions": sum(row.kind == "frequency" for row in rows),
                },
            )
        return len(rows)

    async def measure_drift(self) -> UsageDrift:
        """聚合 Redis/事实投影差异；不持久化或返回任何号码索引。"""

        engine = self._engine()
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT dimension_key,kind,value FROM usage_projection
                    WHERE expires_at>now() ORDER BY dimension_key
                    """
                )
            )
            rows = [
                (str(row["dimension_key"]), str(row["kind"]), int(row["value"]))
                for row in result.mappings()
            ]
        try:
            raw_values = await self.redis.mget([row[0] for row in rows]) if rows else []
        except Exception:
            raw_values = [None] * len(rows)
        aggregates = {
            "quota": [0, 0],
            "frequency": [0, 0],
        }
        for (_, kind, expected), raw in zip(rows, raw_values, strict=True):
            try:
                actual = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                actual = 0
            if actual != expected:
                aggregates[kind][0] += 1
                aggregates[kind][1] += abs(expected - actual)
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
                                "action": "运行 usage-projection-rebuild 后复核",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    },
                )
        return drift

    async def recover_orphans(self, *, older_than_seconds: int = 600) -> int:
        """把崩溃遗留的受理预留转为持久释放事件。"""

        if older_than_seconds < 60:
            raise ValueError("usage orphan threshold too small")
        engine = self._engine()
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id FROM usage_reservation
                    WHERE state IN ('reserved','uncertain')
                      AND updated_at < now()-make_interval(secs=>:seconds)
                    ORDER BY updated_at LIMIT 500
                    """
                ),
                {"seconds": older_than_seconds},
            )
            reservation_ids = [UUID(str(value)) for value in result.scalars()]
        recovered = 0
        for reservation_id in reservation_ids:
            if await self.request_release(
                reservation_id,
                event_id=f"usage:{reservation_id}:orphan-recovery",
            ):
                recovered += 1
        return recovered

    async def explain(
        self,
        *,
        reservation_id: UUID | None = None,
        batch_no: str | None = None,
    ) -> dict[str, Any]:
        """解释计数来源，只返回主体 UUID 和窗口，不返回 HMAC 或手机号。"""

        if (reservation_id is None) == (batch_no is None):
            raise ValueError("provide exactly one usage reservation reference")
        engine = self._engine()
        async with engine.connect() as connection:
            if reservation_id is None:
                value = await connection.scalar(
                    text(
                        """
                        SELECT usage_reservation_id FROM sms_batch
                        WHERE batch_no=:batch_no
                        """
                    ),
                    {"batch_no": batch_no},
                )
                if value is None:
                    raise UsageReservationConflict("usage reservation unavailable")
                reservation_id = UUID(str(value))
            reservation_result = await connection.execute(
                text(
                    """
                    SELECT id,request_key,app_id,dept,category,usage_date,state,
                           quota_cost,release_event_id,reserved_at,
                           committed_at,release_requested_at,released_at
                    FROM usage_reservation WHERE id=:id
                    """
                ),
                {"id": reservation_id},
            )
            reservation = reservation_result.mappings().one_or_none()
            if reservation is None:
                raise UsageReservationConflict("usage reservation unavailable")
            quota_result = await connection.execute(
                text(
                    """
                    SELECT dimension_kind,dimension_value,amount,usage_date
                    FROM usage_quota_entry WHERE reservation_id=:id
                    ORDER BY dimension_kind
                    """
                ),
                {"id": reservation_id},
            )
            frequency_result = await connection.execute(
                text(
                    """
                    SELECT e.subject_id,e.category,e.window_kind,e.window_key,
                           e.counted,e.usage_date
                    FROM usage_frequency_entry e
                    WHERE e.reservation_id=:id
                    ORDER BY e.subject_id,e.window_kind
                    """
                ),
                {"id": reservation_id},
            )
            linked_batch = await connection.scalar(
                text(
                    """
                    SELECT trim(batch_no) FROM sms_batch
                    WHERE usage_reservation_id=:id
                    """
                ),
                {"id": reservation_id},
            )
        return {
            "reservation_id": str(reservation_id),
            "batch_no": str(linked_batch) if linked_batch is not None else None,
            "app_id": int(reservation["app_id"]),
            "dept": str(reservation["dept"]),
            "category": str(reservation["category"]),
            "usage_date": reservation["usage_date"].isoformat(),
            "state": str(reservation["state"]),
            "quota_cost": int(reservation["quota_cost"]),
            "quota_dimensions": [
                {
                    "kind": str(row["dimension_kind"]),
                    "value": str(row["dimension_value"]),
                    "amount": int(row["amount"]),
                }
                for row in quota_result.mappings()
            ],
            "frequency_dimensions": [
                {
                    "subject_id": str(row["subject_id"]),
                    "category": str(row["category"]),
                    "window_kind": str(row["window_kind"]),
                    "window_key": str(row["window_key"]),
                    "counted": bool(row["counted"]),
                }
                for row in frequency_result.mappings()
            ],
            "release_event_id": (
                str(reservation["release_event_id"])
                if reservation["release_event_id"] is not None
                else None
            ),
        }


__all__ = [
    "APPLY_PROJECTION_LUA",
    "ProjectionRow",
    "UsageDrift",
    "UsageLedgerService",
    "UsageProjectionUnavailable",
    "UsageReservation",
    "UsageReservationConflict",
    "commit_usage_reservation",
    "frequency_windows",
    "request_usage_release_for_batch",
    "shanghai_day",
]
