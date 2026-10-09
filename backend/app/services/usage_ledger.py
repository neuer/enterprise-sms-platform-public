"""配额与号码频控的 PostgreSQL 事实账本及可重建 Redis 投影。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.quota import QuotaExceeded
from app.services.usage_ledger_common import (
    DATE_KEY_PATTERN,
    FrequencyDecisionItem,
    UsageDrift,
    UsageProjectionUnavailable,
    UsageRedis,
    UsageReservation,
    UsageReservationConflict,
    _safe_request_key,
    frequency_windows,
    shanghai_day,
    utc_now,
)
from app.services.usage_ledger_frequency import (
    UsageFrequencyMixin,
)
from app.services.usage_ledger_projection import (
    APPLY_PROJECTION_LUA,
    RECONCILE_REBUILD_ACTOR,
    ProjectionRow,
    _change_projections,
    _lock_projection_keys,
    _projection_rows,
    _ProjectionChange,
    reconcile_usage_facts,
)
from app.services.usage_ledger_release import (
    UsageReleaseMixin,
    request_usage_release_for_batch,
)
from app.settings import Settings, get_settings


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


class UsageLedgerService(UsageFrequencyMixin, UsageReleaseMixin):
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

    async def start_reservation(
        self,
        *,
        request_key: str,
        app_id: int,
        dept: str,
        category: str,
        now: datetime | None = None,
        subject_kind: str = "api_app",
    ) -> UsageReservation:
        current = now or self.clock()
        await self.ensure_ready(current)
        request_key = _safe_request_key(request_key)
        _, usage_date, _ = shanghai_day(current)
        if (
            app_id < 0
            or not dept
            or category not in {"verify", "notice", "market"}
            or subject_kind not in {"api_app", "system_effect"}
            or (subject_kind == "system_effect" and app_id < 1)
        ):
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
                          id,request_key,app_id,subject_kind,dept,category,
                          usage_date,state
                        ) VALUES(
                          :id,:request_key,:app_id,:subject_kind,:dept,:category,
                          :usage_date,'reserved'
                        )
                        ON CONFLICT(request_key)
                        WHERE state NOT IN ('released','release_requested')
                        DO UPDATE SET request_key=EXCLUDED.request_key
                        RETURNING id,app_id,subject_kind,dept,category,usage_date,state
                        """
                    ),
                    {
                        "id": reservation_id,
                        "request_key": request_key,
                        "app_id": app_id,
                        "subject_kind": subject_kind,
                        "dept": dept,
                        "category": category,
                        "usage_date": usage_date,
                    },
                )
                row = result.mappings().one()
                expected = (app_id, subject_kind, dept, category, usage_date)
                persisted = (
                    int(row["app_id"]),
                    str(row["subject_kind"]),
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
    "FrequencyDecisionItem",
    "frequency_windows",
    "reconcile_usage_facts",
    "request_usage_release_for_batch",
    "shanghai_day",
]
