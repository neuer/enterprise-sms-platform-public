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

from app.core.runtime_resources import bind_connection_system_audit, database_engine
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
    rf"acceptance:(?:v2:[0-9a-f]{{64}}:[0-9]{{8}}|"
    rf"[0-9]+:[0-9a-f]{{64}}:[0-9]{{8}}|{UUID_FRAGMENT})"
    rf"|legacy:batch:[0-9a-f]{{32}}"
    rf")$"
)
RELEASE_EVENT_ID_PATTERN = re.compile(
    rf"^(?:"
    rf"batch:[0-9a-f]{{32}}:cancelled"
    rf"|approval:[1-9][0-9]*:(?:rejected|expired)"
    rf"|usage:{UUID_FRAGMENT}:"
    rf"(?:acceptance-failed|all-filtered|idempotent-reuse|orphan-recovery|uncertain-retry)"
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
class FrequencyDecisionItem:
    """单次频控决策输入；大批量受理按批合并进同一事务。"""

    phone_hmac: str
    hmac_aliases: Mapping[int, str]


@dataclass(frozen=True, slots=True)
class ResolvedFrequencySubject:
    """一次受理内只解析一次的频控主体；决策阶段不得再查库定位。"""

    phone_hmac: str
    hmac_aliases: Mapping[int, str]
    subject_id: UUID
    projection_hmac: str


FREQUENCY_DECISION_CHUNK = 200


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


_FREQ_VERIFY_KEY = re.compile(r"^freq:v:([0-9a-f]{64}):([md])$")
_FREQ_MARKET_KEY = re.compile(r"^freq:m:(\d+):([0-9a-f]{64}):d$")


def _frequency_projection_keys(
    category: str,
    app_id: int,
    projection_hmac: str,
) -> tuple[str, ...]:
    if category == "verify":
        return (f"freq:v:{projection_hmac}:m", f"freq:v:{projection_hmac}:d")
    return (f"freq:m:{app_id}:{projection_hmac}:d",)


def _canonical_frequency_projection_key(
    dimension_key: str,
    canonical_hmac: str,
) -> str | None:
    """把 source 主体的频控投影键改写到 canonical HMAC；无法识别则返回 None。"""

    if HMAC_PATTERN.fullmatch(canonical_hmac) is None:
        raise ValueError("invalid frequency projection hmac")
    verify = _FREQ_VERIFY_KEY.fullmatch(dimension_key)
    if verify is not None:
        return f"freq:v:{canonical_hmac}:{verify.group(2)}"
    market = _FREQ_MARKET_KEY.fullmatch(dimension_key)
    if market is not None:
        return f"freq:m:{market.group(1)}:{canonical_hmac}:d"
    return None


_ACTIVE_RESERVATION_STATES = (
    "reserved",
    "committed",
    "uncertain",
    "release_requested",
)

# live 归并可接受的未来窗口偏差：1 个分钟/自然日边界是合法跨窗，再往前 fail closed。
FREQUENCY_MERGE_FUTURE_MINUTE_SKEW = 2
FREQUENCY_MERGE_FUTURE_DAY_SKEW = 1


def _frequency_window_sort_key(row: Any) -> tuple[date, int | str]:
    """按 (usage_date, window_key) 比较窗口新旧；数字窗口按整数排，避免 '9'>'10'。"""

    window_key = str(row["window_key"])
    if window_key.isdigit():
        return (row["usage_date"], int(window_key))
    return (row["usage_date"], window_key)


def _choose_frequency_merge_window(
    rows: Sequence[Any],
    *,
    target_key: str,
) -> tuple[str, date, str, datetime, tuple[Any, ...]] | None:
    """从 live 行先选最新 (usage_date, window_key)，同窗内再优先 canonical。"""

    if not rows:
        return None
    newest_key = max(_frequency_window_sort_key(row) for row in rows)
    newest_rows = [row for row in rows if _frequency_window_sort_key(row) == newest_key]
    target_rows = [row for row in newest_rows if str(row["dimension_key"]) == target_key]
    chosen = target_rows[0] if target_rows else newest_rows[0]
    kind = str(chosen["kind"])
    usage_date = chosen["usage_date"]
    window_key = str(chosen["window_key"])
    matching = tuple(
        row
        for row in rows
        if str(row["kind"]) == kind
        and row["usage_date"] == usage_date
        and str(row["window_key"]) == window_key
    )
    if not matching:
        return None
    return kind, usage_date, window_key, max(row["expires_at"] for row in matching), matching


def _frequency_window_is_future_skewed(
    *,
    dimension_key: str,
    usage_date: date,
    window_key: str,
    observed: datetime,
) -> bool:
    """对照数据库权威时钟，判断 live 窗口是否超出可接受的未来偏差。"""

    minute, _, _date_key, current_date, _ = frequency_windows(observed)
    if dimension_key.endswith(":m"):
        if not window_key.isdigit():
            return True
        return int(window_key) - int(minute) > FREQUENCY_MERGE_FUTURE_MINUTE_SKEW
    return (usage_date - current_date).days > FREQUENCY_MERGE_FUTURE_DAY_SKEW


def _release_change_should_apply(
    change: _ProjectionChange,
    *,
    existing_keys: set[str],
    observed: datetime,
) -> bool:
    """投影存在则扣减；过期频控且目标缺失则跳过，避免归并后打空。"""

    if change.dimension_key in existing_keys:
        return True
    if change.kind == "frequency" and change.expires_at <= observed:
        return False
    raise UsageReservationConflict("projection batch update conflict")


async def _list_active_frequency_entry_refs(
    connection: AsyncConnection,
    subject_ids: Sequence[UUID],
) -> list[Any]:
    """未终态 counted 频控明细；删除 source 投影前必须仍有可命中的 projection_key。"""

    if not subject_ids:
        return []
    result = await connection.execute(
        text(
            """
            SELECT e.projection_key,e.window_key,e.usage_date,e.expires_at
            FROM usage_frequency_entry e
            JOIN usage_reservation r ON r.id=e.reservation_id
            WHERE e.subject_id=ANY(CAST(:subject_ids AS uuid[]))
              AND e.counted
              AND r.state=ANY(CAST(:states AS text[]))
            """
        ),
        {
            "subject_ids": list(subject_ids),
            "states": list(_ACTIVE_RESERVATION_STATES),
        },
    )
    return list(result.mappings())


async def _write_expired_canonical_tombstone(
    connection: AsyncConnection,
    *,
    target_key: str,
    source_rows: Sequence[Any],
    entry_refs: Sequence[Any],
) -> ProjectionRow | None:
    """为仍被引用的过期 source 写 canonical 墓碑，供负向释放 UPDATE 命中。

    墓碑 expires_at 保持在过去，rebuild 不会把它投影回 Redis，因此不恢复过期限流。
    """

    chosen = _choose_frequency_merge_window(source_rows, target_key=target_key)
    if chosen is not None:
        kind, usage_date, window_key, expires_at, matching = chosen
        value = sum(int(row["value"]) for row in matching)
    else:
        if not entry_refs:
            return None
        picked = max(
            entry_refs,
            key=lambda row: (row["usage_date"], str(row["window_key"])),
        )
        kind = "frequency"
        usage_date = picked["usage_date"]
        window_key = str(picked["window_key"])
        matching_refs = [
            row
            for row in entry_refs
            if row["usage_date"] == usage_date and str(row["window_key"]) == window_key
        ]
        expires_at = max(row["expires_at"] for row in matching_refs)
        value = len(matching_refs)
    return await _write_absolute_frequency_projection(
        connection,
        dimension_key=target_key,
        kind=kind,
        usage_date=usage_date,
        window_key=window_key,
        value=value,
        expires_at=expires_at,
    )


def _latest_projection_rows(rows: Sequence[ProjectionRow]) -> tuple[ProjectionRow, ...]:
    latest: dict[str, ProjectionRow] = {}
    for row in rows:
        current = latest.get(row.dimension_key)
        if current is None or row.version >= current.version:
            latest[row.dimension_key] = row
    return tuple(sorted(latest.values(), key=lambda item: item.dimension_key))


async def _load_frequency_alias_map(
    connection: AsyncConnection,
    digests: Sequence[str],
) -> dict[str, UUID]:
    unique = list(dict.fromkeys(digests))
    if not unique:
        return {}
    result = await connection.execute(
        text(
            """
            SELECT phone_hmac,subject_id FROM usage_frequency_alias
            WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
            """
        ),
        {"digests": unique},
    )
    return {
        str(row["phone_hmac"]): UUID(str(row["subject_id"]))
        for row in result.mappings()
    }


async def _load_frequency_subject_rows(
    connection: AsyncConnection,
    subject_ids: Sequence[UUID],
) -> dict[UUID, Any]:
    unique = list(dict.fromkeys(subject_ids))
    if not unique:
        return {}
    result = await connection.execute(
        text(
            """
            SELECT id,projection_hmac FROM usage_frequency_subject
            WHERE id=ANY(CAST(:subject_ids AS uuid[]))
            ORDER BY projection_hmac,id
            """
        ),
        {"subject_ids": unique},
    )
    return {UUID(str(row["id"])): row for row in result.mappings()}


async def _bind_frequency_aliases_many(
    connection: AsyncConnection,
    bindings: Sequence[tuple[UUID, Mapping[int, str]]],
) -> None:
    if not bindings:
        return
    alias_payload = json.dumps(
        [
            {
                "subject_id": str(subject_id),
                "key_version": version,
                "phone_hmac": digest,
            }
            for subject_id, hmac_aliases in bindings
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
              alias.subject_id,alias.key_version,alias.phone_hmac
            FROM jsonb_to_recordset(CAST(:aliases AS jsonb)) AS alias(
              subject_id uuid,key_version smallint,phone_hmac char(64)
            )
            ON CONFLICT(phone_hmac) DO NOTHING
            """
        ),
        {"aliases": alias_payload},
    )


def _item_subject_ids(
    item: FrequencyDecisionItem,
    alias_map: Mapping[str, UUID],
) -> list[UUID]:
    found: list[UUID] = []
    seen: set[UUID] = set()
    for digest in item.hmac_aliases.values():
        subject_id = alias_map.get(digest)
        if subject_id is None or subject_id in seen:
            continue
        seen.add(subject_id)
        found.append(subject_id)
    return found


async def _list_frequency_subjects(
    connection: AsyncConnection,
    digests: Sequence[str],
) -> tuple[set[UUID], list[Any]]:
    alias_result = await connection.execute(
        text(
            """
            SELECT DISTINCT subject_id FROM usage_frequency_alias
            WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
            """
        ),
        {"digests": list(digests)},
    )
    subject_ids = {UUID(str(value)) for value in alias_result.scalars()}
    if not subject_ids:
        return set(), []
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
    return subject_ids, list(subject_result.mappings())


async def _list_frequency_projection_rows(
    connection: AsyncConnection,
    hmacs: Sequence[str],
) -> list[Any]:
    unique = [digest for digest in dict.fromkeys(hmacs) if HMAC_PATTERN.fullmatch(digest)]
    if not unique:
        return []
    verify_keys = [key for digest in unique for key in (f"freq:v:{digest}:m", f"freq:v:{digest}:d")]
    result = await connection.execute(
        text(
            """
            SELECT dimension_key,kind,usage_date,window_key,value,version,expires_at
            FROM usage_projection
            WHERE kind='frequency'
              AND (
                dimension_key=ANY(CAST(:verify_keys AS text[]))
                OR dimension_key LIKE ANY(CAST(:market_patterns AS text[]))
              )
            ORDER BY dimension_key
            """
        ),
        {
            "verify_keys": verify_keys,
            "market_patterns": [f"freq:m:%:{digest}:d" for digest in unique],
        },
    )
    return list(result.mappings())


async def _lock_frequency_merge_keys(
    connection: AsyncConnection,
    hmacs: Sequence[str],
    subject_ids: Sequence[UUID],
) -> None:
    if subject_ids:
        # 先按 id 锁仍引用这些主体的预留，再锁投影键。释放路径是预留后投影，
        # 顺序一致才能避免 UPDATE entry 的外键 KEY SHARE 与 FOR UPDATE 死锁。
        await connection.execute(
            text(
                """
                SELECT r.id FROM usage_reservation r
                WHERE EXISTS (
                  SELECT 1 FROM usage_frequency_entry e
                  WHERE e.reservation_id=r.id
                    AND e.subject_id=ANY(CAST(:subject_ids AS uuid[]))
                )
                ORDER BY r.id
                FOR UPDATE OF r
                """
            ),
            {"subject_ids": list(subject_ids)},
        )
    unique = [digest for digest in dict.fromkeys(hmacs) if HMAC_PATTERN.fullmatch(digest)]
    lock_keys = {key for digest in unique for key in (f"freq:v:{digest}:m", f"freq:v:{digest}:d")}
    for row in await _list_frequency_projection_rows(connection, unique):
        source_key = str(row["dimension_key"])
        lock_keys.add(source_key)
        for digest in unique:
            remapped = _canonical_frequency_projection_key(source_key, digest)
            if remapped is not None:
                lock_keys.add(remapped)
    if subject_ids:
        entry_result = await connection.execute(
            text(
                """
                SELECT DISTINCT projection_key FROM usage_frequency_entry
                WHERE subject_id=ANY(CAST(:subject_ids AS uuid[]))
                """
            ),
            {"subject_ids": list(subject_ids)},
        )
        for key in entry_result.scalars():
            source_key = str(key)
            lock_keys.add(source_key)
            for digest in unique:
                remapped = _canonical_frequency_projection_key(source_key, digest)
                if remapped is not None:
                    lock_keys.add(remapped)
    await _lock_projection_keys(connection, sorted(lock_keys), namespace=43)


async def _assert_canonical_holds_newest_window(
    connection: AsyncConnection,
    *,
    target_key: str,
    usage_date: date,
    window_key: str,
    value: int,
) -> None:
    """删除 source 前确认最新窗口计数已落在 canonical 行。"""

    result = await connection.execute(
        text(
            """
            SELECT usage_date,window_key,value
            FROM usage_projection
            WHERE dimension_key=:target_key
            """
        ),
        {"target_key": target_key},
    )
    row = result.mappings().one_or_none()
    if (
        row is None
        or row["usage_date"] != usage_date
        or str(row["window_key"]) != window_key
        or int(row["value"]) != value
    ):
        raise UsageReservationConflict("frequency merge newest window missing on canonical")


async def _write_absolute_frequency_projection(
    connection: AsyncConnection,
    *,
    dimension_key: str,
    kind: str,
    usage_date: date,
    window_key: str,
    value: int,
    expires_at: datetime,
) -> ProjectionRow:
    result = await connection.execute(
        text(
            """
            INSERT INTO usage_projection(
              dimension_key,kind,usage_date,window_key,value,version,expires_at
            ) VALUES(
              :dimension_key,:kind,:usage_date,:window_key,:value,
              nextval('usage_projection_version_seq'),:expires_at
            )
            ON CONFLICT (dimension_key) DO UPDATE SET
              kind=EXCLUDED.kind,
              usage_date=EXCLUDED.usage_date,
              window_key=EXCLUDED.window_key,
              value=EXCLUDED.value,
              version=nextval('usage_projection_version_seq'),
              expires_at=EXCLUDED.expires_at,
              updated_at=now()
            RETURNING dimension_key,kind,usage_date,value,version,expires_at
            """
        ),
        {
            "dimension_key": dimension_key,
            "kind": kind,
            "usage_date": usage_date,
            "window_key": window_key,
            "value": value,
            "expires_at": expires_at,
        },
    )
    row = result.mappings().one()
    return ProjectionRow(
        str(row["dimension_key"]),
        str(row["kind"]),
        row["usage_date"],
        int(row["value"]),
        int(row["version"]),
        row["expires_at"],
    )


async def _merge_frequency_projections(
    connection: AsyncConnection,
    *,
    canonical_hmac: str,
    subject_rows: Sequence[Any],
) -> tuple[ProjectionRow, ...]:
    """在已持有投影键锁后，把 source 主体窗口计数并入 canonical 键。"""

    hmacs = [str(row["projection_hmac"]) for row in subject_rows]
    subject_ids = [UUID(str(row["id"])) for row in subject_rows]
    observed = await _database_now(connection)
    groups: dict[str, list[Any]] = {}
    remaps: dict[str, str] = {}
    for row in await _list_frequency_projection_rows(connection, hmacs):
        source_key = str(row["dimension_key"])
        target_key = _canonical_frequency_projection_key(source_key, canonical_hmac)
        if target_key is None:
            continue
        remaps[source_key] = target_key
        groups.setdefault(target_key, []).append(row)
    entry_result = await connection.execute(
        text(
            """
            SELECT DISTINCT projection_key FROM usage_frequency_entry
            WHERE subject_id=ANY(CAST(:subject_ids AS uuid[]))
            """
        ),
        {"subject_ids": subject_ids},
    )
    for key in entry_result.scalars():
        source_key = str(key)
        target_key = _canonical_frequency_projection_key(source_key, canonical_hmac)
        if target_key is None or source_key == target_key:
            continue
        remaps.setdefault(source_key, target_key)
    entry_refs = await _list_active_frequency_entry_refs(connection, subject_ids)
    referenced_keys = {str(row["projection_key"]) for row in entry_refs}
    needed_targets = {
        remaps[source_key]
        for source_key in referenced_keys
        if source_key in remaps
    } | {key for key in referenced_keys if key not in remaps}

    written: list[ProjectionRow] = []
    stale_keys: list[str] = []
    ensured_targets: set[str] = set()
    live_expectations: list[tuple[str, date, str, int]] = []
    for target_key, group in groups.items():
        source_keys = [
            str(row["dimension_key"]) for row in group if str(row["dimension_key"]) != target_key
        ]
        stale_keys.extend(source_keys)
        live = [
            row for row in group if row["expires_at"] is not None and row["expires_at"] > observed
        ]
        if live:
            chosen = _choose_frequency_merge_window(live, target_key=target_key)
            if chosen is None:
                continue
            kind, usage_date, window_key, expires_at, matching = chosen
            if _frequency_window_is_future_skewed(
                dimension_key=target_key,
                usage_date=usage_date,
                window_key=window_key,
                observed=observed,
            ):
                raise UsageReservationConflict("frequency merge window clock skew")
            expected_value = sum(int(row["value"]) for row in matching)
            live_expectations.append((target_key, usage_date, window_key, expected_value))
            ensured_targets.add(target_key)
            if not source_keys and len(matching) == 1:
                continue
            written.append(
                await _write_absolute_frequency_projection(
                    connection,
                    dimension_key=target_key,
                    kind=kind,
                    usage_date=usage_date,
                    window_key=window_key,
                    value=expected_value,
                    expires_at=expires_at,
                )
            )
            continue
        if target_key not in needed_targets:
            continue
        related_refs = [
            row
            for row in entry_refs
            if str(row["projection_key"]) in {*source_keys, target_key}
            or remaps.get(str(row["projection_key"])) == target_key
        ]
        tombstone = await _write_expired_canonical_tombstone(
            connection,
            target_key=target_key,
            source_rows=group,
            entry_refs=related_refs,
        )
        if tombstone is not None:
            written.append(tombstone)
            ensured_targets.add(target_key)
    for source_key, target_key in remaps.items():
        if source_key == target_key or target_key in ensured_targets:
            continue
        if target_key not in needed_targets:
            continue
        related_refs = [
            row
            for row in entry_refs
            if str(row["projection_key"]) in {source_key, target_key}
        ]
        tombstone = await _write_expired_canonical_tombstone(
            connection,
            target_key=target_key,
            source_rows=(),
            entry_refs=related_refs,
        )
        if tombstone is not None:
            written.append(tombstone)
            ensured_targets.add(target_key)
    stale_keys.extend(
        source_key for source_key, target_key in remaps.items() if source_key != target_key
    )
    protected_sources = {
        source_key
        for source_key, target_key in remaps.items()
        if source_key in referenced_keys and target_key not in ensured_targets
    }
    for source_key, target_key in sorted(remaps.items()):
        if source_key == target_key or source_key in protected_sources:
            continue
        await connection.execute(
            text(
                """
                UPDATE usage_frequency_entry
                SET projection_key=:target_key
                WHERE projection_key=:source_key
                """
            ),
            {"source_key": source_key, "target_key": target_key},
        )
    unique_stale = sorted({key for key in stale_keys if key not in protected_sources})
    for target_key, usage_date, window_key, value in live_expectations:
        await _assert_canonical_holds_newest_window(
            connection,
            target_key=target_key,
            usage_date=usage_date,
            window_key=window_key,
            value=value,
        )
    if unique_stale:
        await connection.execute(
            text(
                """
                DELETE FROM usage_projection
                WHERE dimension_key=ANY(CAST(:keys AS text[]))
                """
            ),
            {"keys": unique_stale},
        )
    return tuple(written)


async def _database_now(connection: AsyncConnection) -> datetime:
    value = await connection.scalar(text("SELECT now()"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise UsageReservationConflict("database clock unavailable")
    return value


async def _bind_frequency_aliases(
    connection: AsyncConnection,
    *,
    subject_id: UUID,
    hmac_aliases: Mapping[int, str],
) -> None:
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


async def _ensure_frequency_subjects_many(
    connection: AsyncConnection,
    items: Sequence[FrequencyDecisionItem],
) -> tuple[list[ResolvedFrequencySubject], tuple[ProjectionRow, ...]]:
    """整块定位/创建频控主体；SQL 次数随块数增长，不随号码线性放大。"""

    if not items:
        return [], ()
    unique_items: list[FrequencyDecisionItem] = []
    original_to_unique: list[int] = []
    seen_hmac: dict[str, int] = {}
    for item in items:
        existing_index = seen_hmac.get(item.phone_hmac)
        if existing_index is not None:
            original_to_unique.append(existing_index)
            continue
        seen_hmac[item.phone_hmac] = len(unique_items)
        original_to_unique.append(len(unique_items))
        unique_items.append(item)
    all_digests = [
        digest for item in unique_items for digest in item.hmac_aliases.values()
    ]
    alias_map = await _load_frequency_alias_map(connection, all_digests)
    found_subject_ids = [
        subject_id
        for item in unique_items
        for subject_id in _item_subject_ids(item, alias_map)
    ]
    subject_rows = await _load_frequency_subject_rows(connection, found_subject_ids)

    resolved: list[ResolvedFrequencySubject | None] = [None] * len(unique_items)
    merged_rows: list[ProjectionRow] = []
    create_rows: list[tuple[int, FrequencyDecisionItem, UUID, str]] = []
    bind_rows: list[tuple[int, FrequencyDecisionItem, UUID, str]] = []
    for index, item in enumerate(unique_items):
        found = _item_subject_ids(item, alias_map)
        if not found:
            subject_id = uuid4()
            projection_hmac = item.hmac_aliases[min(item.hmac_aliases)]
            create_rows.append((index, item, subject_id, projection_hmac))
            continue
        if len(found) > 1:
            subject_id, projection_hmac, merged = await _ensure_frequency_subject(
                connection, item
            )
            merged_rows.extend(merged)
            resolved[index] = ResolvedFrequencySubject(
                item.phone_hmac,
                item.hmac_aliases,
                subject_id,
                projection_hmac,
            )
            continue
        subject_id = found[0]
        row = subject_rows.get(subject_id)
        if row is None:
            raise UsageReservationConflict("frequency subject unavailable")
        bind_rows.append((index, item, subject_id, str(row["projection_hmac"])))

    if create_rows:
        await connection.execute(
            text(
                """
                INSERT INTO usage_frequency_subject(id,projection_hmac)
                SELECT item.id,item.projection_hmac
                FROM jsonb_to_recordset(CAST(:subjects AS jsonb)) AS item(
                  id uuid,projection_hmac char(64)
                )
                """
            ),
            {
                "subjects": json.dumps(
                    [
                        {
                            "id": str(subject_id),
                            "projection_hmac": projection_hmac,
                        }
                        for _index, _item, subject_id, projection_hmac in create_rows
                    ],
                    separators=(",", ":"),
                )
            },
        )
        bind_rows.extend(create_rows)

    await _bind_frequency_aliases_many(
        connection,
        [(subject_id, item.hmac_aliases) for _index, item, subject_id, _hmac in bind_rows],
    )
    if bind_rows:
        verify_digests = [
            digest
            for _index, item, _sid, _hmac in bind_rows
            for digest in item.hmac_aliases.values()
        ]
        verify_map = await _load_frequency_alias_map(connection, verify_digests)
        for index, item, intended_id, projection_hmac in bind_rows:
            found = _item_subject_ids(item, verify_map)
            if found != [intended_id]:
                raise UsageReservationConflict("frequency alias write conflict")
            resolved[index] = ResolvedFrequencySubject(
                item.phone_hmac,
                item.hmac_aliases,
                intended_id,
                projection_hmac,
            )

    if any(item is None for item in resolved):
        raise UsageReservationConflict("frequency subject unavailable")
    unique_resolved = [item for item in resolved if item is not None]
    return [unique_resolved[index] for index in original_to_unique], tuple(merged_rows)


async def _ensure_frequency_subject(
    connection: AsyncConnection,
    item: FrequencyDecisionItem,
) -> tuple[UUID, str, tuple[ProjectionRow, ...]]:
    """定位或创建频控主体，必要时原子归并投影，返回 (subject, hmac, 变更投影)。"""

    hmac_aliases = dict(item.hmac_aliases)
    digests = list(hmac_aliases.values())
    subject_ids, subject_rows = await _list_frequency_subjects(connection, digests)
    merged: tuple[ProjectionRow, ...] = ()
    if subject_ids:
        if len(subject_rows) != len(subject_ids):
            raise UsageReservationConflict("frequency subject unavailable")
        preferred_subject = await connection.scalar(
            text(
                """
                SELECT subject_id FROM usage_frequency_alias
                WHERE phone_hmac=:preferred_digest
                """
            ),
            {"preferred_digest": hmac_aliases[min(hmac_aliases)]},
        )
        if len(subject_rows) > 1:
            await _lock_frequency_merge_keys(
                connection,
                [str(row["projection_hmac"]) for row in subject_rows],
                [UUID(str(row["id"])) for row in subject_rows],
            )
            subject_ids, subject_rows = await _list_frequency_subjects(connection, digests)
            if len(subject_rows) != len(subject_ids):
                raise UsageReservationConflict("frequency subject unavailable")
            preferred_subject = await connection.scalar(
                text(
                    """
                    SELECT subject_id FROM usage_frequency_alias
                    WHERE phone_hmac=:preferred_digest
                    """
                ),
                {"preferred_digest": hmac_aliases[min(hmac_aliases)]},
            )
        preferred_subject_id = (
            UUID(str(preferred_subject)) if preferred_subject is not None else None
        )
        canonical = next(
            (row for row in subject_rows if UUID(str(row["id"])) == preferred_subject_id),
            subject_rows[0],
        )
        subject_id = UUID(str(canonical["id"]))
        projection_hmac = str(canonical["projection_hmac"])
        if len(subject_rows) > 1:
            merged = await _merge_frequency_projections(
                connection,
                canonical_hmac=projection_hmac,
                subject_rows=subject_rows,
            )
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
    await _bind_frequency_aliases(
        connection,
        subject_id=subject_id,
        hmac_aliases=hmac_aliases,
    )
    return subject_id, projection_hmac, merged


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


async def _collect_release_projection_changes(
    connection: AsyncConnection,
    reservation_id: UUID,
) -> list[_ProjectionChange]:
    """按当前明细汇总负向投影变更；调用方必须已持有预留行锁。"""

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
    return changes


async def _apply_release_projection_changes(
    connection: AsyncConnection,
    reservation_id: UUID,
) -> tuple[ProjectionRow, ...]:
    """按首次读到的键排序加锁后重读明细再扣减。

    不得在已持有 source 锁后再锁 canonical，否则会与归并的按序锁形成死锁。
    重读后若键已被改写：目标存在则直接扣减，过期且缺失则跳过。
    """

    observed = await _database_now(connection)
    initial = await _collect_release_projection_changes(connection, reservation_id)
    await _lock_projection_keys(
        connection,
        [change.dimension_key for change in initial if change.kind == "frequency"],
        namespace=43,
    )
    await _lock_projection_keys(
        connection,
        [change.dimension_key for change in initial if change.kind == "quota"],
        namespace=47,
    )
    changes = await _collect_release_projection_changes(connection, reservation_id)
    existing = {
        row.dimension_key
        for row in await _projection_rows(
            connection,
            [change.dimension_key for change in changes],
        )
    }
    applicable = [
        change
        for change in changes
        if _release_change_should_apply(
            change,
            existing_keys=existing,
            observed=observed,
        )
    ]
    return await _change_projections(connection, applicable)


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
        rows = await _apply_release_projection_changes(connection, reservation_id)
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

    async def _claim_rebuild_lock(self, date_key: str) -> Any:
        """PostgreSQL advisory lock 是重建 Owner；Redis 键只作 300 秒可见进度。"""

        connection = await self._engine().connect()
        try:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))"),
                {"name": "usage:projection:rebuild"},
            )
        except Exception:
            await connection.close()
            raise
        if not locked:
            await connection.close()
            raise UsageProjectionUnavailable("usage projection rebuild in progress")
        try:
            owns = await self.redis.set(
                f"usage:projection:rebuild:{date_key}",
                str(uuid4()),
                nx=True,
                ex=300,
            )
        except Exception as exc:
            await self._release_rebuild_lock(connection)
            raise UsageProjectionUnavailable("usage projection redis unavailable") from exc
        if not owns:
            await self._release_rebuild_lock(connection)
            raise UsageProjectionUnavailable("usage projection rebuild in progress")
        return connection

    async def _release_rebuild_lock(self, connection: Any) -> None:
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))"),
                {"name": "usage:projection:rebuild"},
            )
        finally:
            await connection.close()

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
            connection = await self._claim_rebuild_lock(date_key)
            try:
                await self.rebuild(actor=RECONCILE_REBUILD_ACTOR)
            finally:
                await self._release_rebuild_lock(connection)
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
        # uncertain 复用最多释放一次旧行后重建；有界循环替代递归，
        # 终止性不依赖状态机偶然性。
        for _ in range(2):
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
                        ON CONFLICT(request_key)
                        WHERE state NOT IN ('released','release_requested')
                        DO UPDATE SET request_key=EXCLUDED.request_key
                        RETURNING id,app_id,dept,category,usage_date,state
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
                reused = persisted_id != reservation_id
                reused_uncertain = reused and str(row["state"]) == "uncertain"
            if not reused_uncertain:
                return UsageReservation(
                    persisted_id,
                    reused=reused,
                )
            # 旧 uncertain 行转入 release_requested 排水后不再占用活跃唯一
            # 索引，下一轮 INSERT 即可重建全新预留。
            await self.request_unlinked_release(
                persisted_id,
                event_id=f"usage:{persisted_id}:uncertain-retry",
            )
        raise UsageReservationConflict("usage reservation retry drained")

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

        results = await self.allow_frequency_many(
            reservation_id,
            category,
            app_id=app_id,
            items=(
                FrequencyDecisionItem(
                    phone_hmac=phone_hmac,
                    hmac_aliases=dict(hmac_aliases),
                ),
            ),
            limits=limits,
            now=now,
        )
        return results[0]

    async def allow_frequency_many(
        self,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        items: Sequence[FrequencyDecisionItem],
        limits: FrequencyLimits,
        now: datetime | None = None,
    ) -> list[bool]:
        """将一批号码的频控决策合并为少量事务，避免万级逐号提交。"""

        if category == "notice":
            return [True] * len(items)
        if category not in {"verify", "market"}:
            raise ValueError("unsupported frequency category")
        validated: list[FrequencyDecisionItem] = []
        for item in items:
            aliases = dict(item.hmac_aliases)
            if HMAC_PATTERN.fullmatch(item.phone_hmac) is None:
                raise ValueError("phone_hmac must be 64 lowercase hex characters")
            if (
                not aliases
                or any(
                    version < 1 or HMAC_PATTERN.fullmatch(digest) is None
                    for version, digest in aliases.items()
                )
                or item.phone_hmac not in aliases.values()
            ):
                raise ValueError("invalid frequency hmac aliases")
            validated.append(FrequencyDecisionItem(item.phone_hmac, aliases))
        if not validated:
            return []
        current = now or self.clock()
        await self.ensure_ready(current)
        windows = frequency_windows(current)
        results: list[bool] = []
        for offset in range(0, len(validated), FREQUENCY_DECISION_CHUNK):
            chunk = validated[offset : offset + FREQUENCY_DECISION_CHUNK]
            allowed, rows = await self._allow_frequency_chunk(
                reservation_id,
                category,
                app_id=app_id,
                items=chunk,
                limits=limits,
                windows=windows,
            )
            try:
                await self._apply_rows(rows)
            except UsageProjectionUnavailable as exc:
                await self._mark_uncertain(reservation_id, type(exc).__name__)
                raise
            results.extend(allowed)
        return results

    async def _allow_frequency_chunk(
        self,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        items: Sequence[FrequencyDecisionItem],
        limits: FrequencyLimits,
        windows: tuple[str, datetime, str, date, datetime],
    ) -> tuple[list[bool], tuple[ProjectionRow, ...]]:
        minute_window, minute_expires, day_window, usage_date, day_expires = windows
        engine = self._engine()
        async with engine.begin() as connection:
            await _lock_projection_keys(
                connection,
                [digest for item in items for digest in item.hmac_aliases.values()],
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
            await connection.execute(
                text(
                    """
                    UPDATE usage_reservation SET updated_at=now()
                    WHERE id=:id AND state='reserved'
                    """
                ),
                {"id": reservation_id},
            )
            # 整块只解析一次主体，再按序加锁；决策不得再次 _ensure。
            resolved, merged_rows = await _ensure_frequency_subjects_many(
                connection, items
            )
            freq_keys = [
                key
                for item in resolved
                for key in _frequency_projection_keys(
                    category, app_id, item.projection_hmac
                )
            ]
            await _lock_projection_keys(
                connection,
                freq_keys,
                namespace=43,
            )
            allowed, rows = await self._decide_frequency_many_on_connection(
                connection,
                reservation_id,
                category,
                app_id=app_id,
                items=resolved,
                limits=limits,
                minute_window=minute_window,
                minute_expires=minute_expires,
                day_window=day_window,
                usage_date=usage_date,
                day_expires=day_expires,
            )
            return allowed, _latest_projection_rows((*merged_rows, *rows))

    async def _decide_frequency_many_on_connection(
        self,
        connection: AsyncConnection,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        items: Sequence[ResolvedFrequencySubject],
        limits: FrequencyLimits,
        minute_window: str,
        minute_expires: datetime,
        day_window: str,
        usage_date: date,
        day_expires: datetime,
    ) -> tuple[list[bool], tuple[ProjectionRow, ...]]:
        """整块频控决策：已解析主体只读窗口、计数、写入，不再逐号定位主体。"""

        if not items:
            return [], ()
        expected_windows = ("minute", "day") if category == "verify" else ("day",)
        existing = await connection.execute(
            text(
                """
                SELECT subject_id,window_kind,counted,projection_key
                FROM usage_frequency_entry
                WHERE reservation_id=:reservation_id
                  AND subject_id=ANY(CAST(:subject_ids AS uuid[]))
                """
            ),
            {
                "reservation_id": reservation_id,
                "subject_ids": [item.subject_id for item in items],
            },
        )
        existing_by_subject: dict[UUID, dict[str, bool]] = {}
        replay_keys: list[str] = []
        for row in existing.mappings():
            subject_id = UUID(str(row["subject_id"]))
            windows = existing_by_subject.setdefault(subject_id, {})
            windows[str(row["window_kind"])] = bool(row["counted"])
            if bool(row["counted"]):
                replay_keys.append(str(row["projection_key"]))

        replay_items: list[tuple[int, ResolvedFrequencySubject, dict[str, bool]]] = []
        new_items: list[tuple[int, ResolvedFrequencySubject]] = []
        for index, item in enumerate(items):
            existing_rows = existing_by_subject.get(item.subject_id, {})
            if set(existing_rows) == set(expected_windows):
                replay_items.append((index, item, existing_rows))
                continue
            if existing_rows:
                raise UsageReservationConflict("partial frequency decision persisted")
            new_items.append((index, item))

        allowed = [False] * len(items)
        rows: list[ProjectionRow] = []
        if replay_items:
            rows.extend(await _projection_rows(connection, replay_keys))
            for index, _item, existing_rows in replay_items:
                allowed[index] = all(existing_rows.values())
        if new_items:
            new_allowed, changed = await self._insert_frequency_decisions(
                connection,
                reservation_id,
                category,
                app_id=app_id,
                items=new_items,
                limits=limits,
                minute_window=minute_window,
                minute_expires=minute_expires,
                day_window=day_window,
                usage_date=usage_date,
                day_expires=day_expires,
            )
            for index, decision in new_allowed:
                allowed[index] = decision
            rows.extend(changed)
        return allowed, tuple(rows)

    async def _insert_frequency_decisions(
        self,
        connection: AsyncConnection,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        items: Sequence[tuple[int, ResolvedFrequencySubject]],
        limits: FrequencyLimits,
        minute_window: str,
        minute_expires: datetime,
        day_window: str,
        usage_date: date,
        day_expires: datetime,
    ) -> tuple[list[tuple[int, bool]], tuple[ProjectionRow, ...]]:
        spec_rows: list[dict[str, object]] = []
        item_specs: list[
            tuple[int, ResolvedFrequencySubject, tuple[tuple[str, str, str, int, datetime], ...]]
        ] = []
        unique_items: list[tuple[int, ResolvedFrequencySubject]] = []
        seen_subjects: set[UUID] = set()
        for index, item in items:
            if item.subject_id in seen_subjects:
                continue
            seen_subjects.add(item.subject_id)
            unique_items.append((index, item))
        for index, item in unique_items:
            keys = _frequency_projection_keys(category, app_id, item.projection_hmac)
            specs: tuple[tuple[str, str, str, int, datetime], ...]
            if category == "verify":
                specs = (
                    (
                        "minute",
                        minute_window,
                        keys[0],
                        limits.verify_per_minute,
                        minute_expires,
                    ),
                    (
                        "day",
                        day_window,
                        keys[1],
                        limits.verify_per_day,
                        day_expires,
                    ),
                )
            else:
                specs = (
                    (
                        "day",
                        day_window,
                        keys[0],
                        limits.market_per_day,
                        day_expires,
                    ),
                )
            item_specs.append((index, item, specs))
            for window_kind, window_key, key, _limit, expires_at in specs:
                spec_rows.append(
                    {
                        "subject_id": str(item.subject_id),
                        "window_kind": window_kind,
                        "window_key": window_key,
                        "projection_key": key,
                        "expires_at": expires_at.isoformat(),
                    }
                )
        spec_payload = json.dumps(spec_rows, separators=(",", ":"))
        count_result = await connection.execute(
            text(
                """
                WITH expected AS (
                  SELECT *
                  FROM jsonb_to_recordset(CAST(:specs AS jsonb)) AS item(
                    subject_id uuid,window_kind text,window_key text,
                    projection_key text,expires_at timestamptz
                  )
                )
                SELECT
                  expected.subject_id,
                  expected.projection_key,
                  count(e.reservation_id)::bigint value
                FROM expected
                LEFT JOIN (
                  usage_frequency_entry e
                  JOIN usage_reservation r
                    ON r.id=e.reservation_id
                   AND r.state IN ('reserved','committed','uncertain')
                ) ON e.subject_id=expected.subject_id
                  AND e.category=:category
                  AND e.app_id IS NOT DISTINCT FROM :frequency_app_id
                  AND e.window_kind=expected.window_kind
                  AND e.window_key=expected.window_key
                  AND e.counted
                GROUP BY expected.subject_id,expected.projection_key
                """
            ),
            {
                "specs": spec_payload,
                "category": category,
                "frequency_app_id": app_id if category == "market" else None,
            },
        )
        counts: dict[tuple[UUID, str], int] = {
            (UUID(str(item["subject_id"])), str(item["projection_key"])): int(item["value"])
            for item in count_result.mappings()
        }
        decisions: list[tuple[int, bool]] = []
        entry_payload: list[dict[str, object]] = []
        changes: list[_ProjectionChange] = []
        for index, item, specs in item_specs:
            allowed = all(
                counts[(item.subject_id, key)] + 1 <= limit
                for _window_kind, _window_key, key, limit, _expires_at in specs
            )
            decisions.append((index, allowed))
            if allowed:
                for _window_kind, window_key, key, _limit, expires_at in specs:
                    counts[(item.subject_id, key)] += 1
                    changes.append(
                        _ProjectionChange(
                            dimension_key=key,
                            kind="frequency",
                            usage_date=usage_date,
                            window_key=window_key,
                            delta=1,
                            expires_at=expires_at,
                        )
                    )
            for window_kind, window_key, key, _limit, expires_at in specs:
                entry_payload.append(
                    {
                        "subject_id": str(item.subject_id),
                        "window_kind": window_kind,
                        "window_key": window_key,
                        "projection_key": key,
                        "counted": allowed,
                        "expires_at": expires_at.isoformat(),
                    }
                )
        await connection.execute(
            text(
                """
                INSERT INTO usage_frequency_entry(
                  reservation_id,subject_id,app_id,category,
                  window_kind,window_key,usage_date,projection_key,
                  counted,expires_at
                )
                SELECT
                  :reservation_id,item.subject_id,:app_id,:category,
                  item.window_kind,item.window_key,:usage_date,
                  item.projection_key,item.counted,item.expires_at
                FROM jsonb_to_recordset(CAST(:entries AS jsonb)) AS item(
                  subject_id uuid,window_kind text,window_key text,
                  projection_key text,counted boolean,expires_at timestamptz
                )
                """
            ),
            {
                "reservation_id": reservation_id,
                "app_id": app_id if category == "market" else None,
                "category": category,
                "usage_date": usage_date,
                "entries": json.dumps(entry_payload, separators=(",", ":")),
            },
        )
        merged_changes: dict[str, _ProjectionChange] = {}
        for change in changes:
            current = merged_changes.get(change.dimension_key)
            if current is None:
                merged_changes[change.dimension_key] = change
                continue
            merged_changes[change.dimension_key] = _ProjectionChange(
                dimension_key=change.dimension_key,
                kind=change.kind,
                usage_date=change.usage_date,
                window_key=change.window_key,
                delta=current.delta + change.delta,
                expires_at=max(current.expires_at, change.expires_at),
            )
        rows = (
            await _change_projections(connection, tuple(merged_changes.values()))
            if merged_changes
            else ()
        )
        allowed_by_subject = {
            item.subject_id: allowed
            for (_index, item, _specs), (_idx, allowed) in zip(
                item_specs, decisions, strict=True
            )
        }
        return [
            (index, allowed_by_subject[item.subject_id]) for index, item in items
        ], rows

    async def _decide_frequency_on_connection(
        self,
        connection: AsyncConnection,
        reservation_id: UUID,
        category: str,
        *,
        app_id: int,
        item: FrequencyDecisionItem,
        limits: FrequencyLimits,
        minute_window: str,
        minute_expires: datetime,
        day_window: str,
        usage_date: date,
        day_expires: datetime,
        lock_keys: bool = True,
    ) -> tuple[bool, tuple[ProjectionRow, ...]]:
        subject_id, projection_hmac, _merged = await _ensure_frequency_subject(connection, item)
        if lock_keys:
            await _lock_projection_keys(
                connection,
                _frequency_projection_keys(category, app_id, projection_hmac),
                namespace=43,
            )
        allowed, rows = await self._decide_frequency_many_on_connection(
            connection,
            reservation_id,
            category,
            app_id=app_id,
            items=(
                ResolvedFrequencySubject(
                    item.phone_hmac,
                    item.hmac_aliases,
                    subject_id,
                    projection_hmac,
                ),
            ),
            limits=limits,
            minute_window=minute_window,
            minute_expires=minute_expires,
            day_window=day_window,
            usage_date=usage_date,
            day_expires=day_expires,
        )
        return allowed[0], rows

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

    async def request_unlinked_release(
        self,
        reservation_id: UUID,
        *,
        event_id: str,
    ) -> bool:
        """仅补偿尚未绑定批次的受理预留；与批次提交按行锁串行。"""

        engine = self._engine()
        async with engine.begin() as connection:
            selected = await connection.execute(
                text(
                    """
                    SELECT state FROM usage_reservation
                    WHERE id=:reservation_id FOR UPDATE
                    """
                ),
                {"reservation_id": reservation_id},
            )
            row = selected.mappings().one_or_none()
            if row is None:
                raise UsageReservationConflict("usage reservation unavailable")
            # 单独语句获得锁后的新 READ COMMITTED 快照；避免等待并发 batch
            # 提交时，LEFT JOIN 沿用等待前快照而误判为未绑定。
            linked = await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM sms_batch "
                    "WHERE usage_reservation_id=:reservation_id)"
                ),
                {"reservation_id": reservation_id},
            )
            if linked:
                return False
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
        # Redis 不可读时没有“当前投影”这一事实；必须让任务失败并由当前告警
        # 标记 UNKNOWN，不能把依赖故障折算成一组看似有效的零值。
        raw_values = await self.redis.mget([row[0] for row in rows]) if rows else []
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
                                "action": "巡检将按事实覆盖 Redis 投影后复核",
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
            stuck_result = await connection.execute(
                text(
                    """
                    SELECT id FROM usage_reservation
                    WHERE state='release_requested'
                      AND updated_at < now()-make_interval(secs=>:seconds)
                    ORDER BY updated_at LIMIT 500
                    """
                ),
                {"seconds": older_than_seconds},
            )
            stuck_ids = [UUID(str(value)) for value in stuck_result.scalars()]
        recovered = 0
        for reservation_id in reservation_ids:
            try:
                # 只回收尚未绑定批次的预留：扫描后、拿到行锁前预留可能已被
                # 并发批次提交为 committed，无绑定检查的释放会造成配额双花。
                released = await self.request_unlinked_release(
                    reservation_id,
                    event_id=f"usage:{reservation_id}:orphan-recovery",
                )
            except UsageReservationConflict:
                # 单行竞态（并发终态迁移/事件已占用）不阻断整轮回收。
                continue
            if released:
                recovered += 1
        for reservation_id in stuck_ids:
            try:
                # usage.release 事件死信后 apply 永不再来（#350）：直接重驱
                # apply_release。它按 DB 事实覆盖绝对投影且仅在
                # release_requested 状态迁移，与迟到的 Outbox 消费幂等共存。
                recovered += await self.apply_release(reservation_id)
            except (UsageReservationConflict, UsageProjectionUnavailable):
                continue
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
    "RECONCILE_REBUILD_ACTOR",
    "UsageDrift",
    "UsageLedgerService",
    "UsageProjectionUnavailable",
    "UsageReservation",
    "UsageReservationConflict",
    "commit_usage_reservation",
    "frequency_windows",
    "reconcile_usage_facts",
    "request_usage_release_for_batch",
    "shanghai_day",
]
