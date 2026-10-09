"""号码频控决策：按事实账本串行判定并写入频控明细与投影。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.freq import FrequencyLimits
from app.services.usage_ledger_common import (
    FREQUENCY_DECISION_CHUNK,
    HMAC_PATTERN,
    FrequencyDecisionItem,
    ResolvedFrequencySubject,
    UsageProjectionUnavailable,
    UsageReservationConflict,
    frequency_windows,
)
from app.services.usage_ledger_frequency_merge import (
    _frequency_projection_keys,
)
from app.services.usage_ledger_frequency_subjects import (
    _ensure_frequency_subject,
    _ensure_frequency_subjects_many,
)
from app.services.usage_ledger_projection import (
    ProjectionRow,
    UsageProjectionMixin,
    _change_projections,
    _latest_projection_rows,
    _lock_projection_keys,
    _projection_rows,
    _ProjectionChange,
)


class UsageFrequencyMixin(UsageProjectionMixin):
    """UsageLedgerService 的号码频控；只对已接受号码计数。"""

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
            resolved, merged_rows = await _ensure_frequency_subjects_many(connection, items)
            freq_keys = [
                key
                for item in resolved
                for key in _frequency_projection_keys(category, app_id, item.projection_hmac)
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
            for (_index, item, _specs), (_idx, allowed) in zip(item_specs, decisions, strict=True)
        }
        return [(index, allowed_by_subject[item.subject_id]) for index, item in items], rows

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
