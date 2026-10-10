"""号码频控投影的规范键与 HMAC 轮换后的窗口合并（不可逆 alias 归并同一主体）。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.usage_ledger_common import (
    HMAC_PATTERN,
    UsageReservationConflict,
    frequency_windows,
)
from app.services.usage_ledger_projection import (
    ProjectionRow,
    _database_now,
    _lock_projection_keys,
    _lock_projection_writer,
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
    await _lock_projection_writer(connection)
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
    await _lock_projection_writer(connection)
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
        remaps[source_key] for source_key in referenced_keys if source_key in remaps
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
            row for row in entry_refs if str(row["projection_key"]) in {source_key, target_key}
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
