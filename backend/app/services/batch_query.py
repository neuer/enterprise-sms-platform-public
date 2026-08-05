"""批次与消息明细的最小权限查询服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings


class BatchNotFound(LookupError):
    """批次不存在或不在调用方数据权限内。"""


@dataclass(frozen=True, slots=True)
class BatchAccessScope:
    """API Key 限定应用；Web 用户限定部门，管理员可查看全部。"""

    app_id: int | None = None
    dept: str | None = None
    all_departments: bool = False

    def sql(self) -> tuple[str, dict[str, object]]:
        if self.app_id is not None:
            return "b.app_id=:scope_app_id", {"scope_app_id": self.app_id}
        if self.dept is not None:
            return "b.dept=:scope_dept", {"scope_dept": self.dept}
        if self.all_departments:
            return "TRUE", {}
        raise ValueError("batch query scope must be constrained")


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符（PostgreSQL 默认反斜杠转义）。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class BatchQueryService:
    """查询永不选择手机号密文/HMAC，响应只能使用 phone_mask。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_batches(
        self,
        *,
        scope: BatchAccessScope,
        category: str | None,
        status: str | None,
        channel: str | None,
        app_id: int | None,
        is_test: bool | None,
        batch_no: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        size: int,
    ) -> dict[str, object]:
        """按权限与运营筛选分页；只返回批次级无手机号字段。"""

        if page < 1 or size < 1:
            raise ValueError("page and size must be positive")
        for moment in (start, end):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError("query time must include timezone")
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be later than end")
        predicate, scope_params = scope.sql()
        clauses = [predicate]
        params: dict[str, object] = {
            "limit": size,
            "offset": (page - 1) * size,
            **scope_params,
        }
        filters: tuple[tuple[str, object | None, str], ...] = (
            ("category", category, "b.category=:category"),
            ("status", status, "b.status=:status"),
            ("channel", channel, "b.channel=:channel"),
            ("app_id", app_id, "b.app_id=:app_id"),
            ("is_test", is_test, "b.is_test=:is_test"),
            ("start", start, "b.created_at>=:start"),
            ("end", end, "b.created_at<=:end"),
        )
        for name, value, clause in filters:
            if value is not None:
                clauses.append(clause)
                params[name] = value
        if batch_no is not None and batch_no.strip():
            clauses.append("trim(b.batch_no) ILIKE :batch_no")
            params["batch_no"] = f"%{_escape_like(batch_no.strip())}%"
        where = "\n AND ".join(clauses)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count_result = await connection.execute(
                    text("SELECT count(*) FROM sms_batch b WHERE " + where),
                    params,
                )
                rows_result = await connection.execute(
                    text(
                        """
                        SELECT trim(b.batch_no) AS batch_no,b.category,b.channel,
                          a.name AS app_name,b.creator,b.dept,b.content,b.status,
                          b.deferred_reason,trim(original.batch_no) AS resend_of,
                          b.is_test,b.segments,b.quota_cost,b.total,
                          b.removed_freq AS removed_freq_limit,b.delivered,b.failed,
                          b.unknown_cnt AS unknown,b.scheduled_at,b.created_at
                        FROM sms_batch b LEFT JOIN app a ON a.id=b.app_id
                        LEFT JOIN sms_batch original ON original.id=b.resend_of
                        WHERE """
                        + where
                        + " ORDER BY b.created_at DESC,b.id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                return {
                    "total": int(count_result.scalar_one()),
                    "items": [dict(row) for row in rows_result.mappings()],
                }
        finally:
            await engine.dispose()

    async def get_batch(
        self,
        batch_no: str,
        scope: BatchAccessScope,
    ) -> dict[str, object]:
        predicate, scope_params = scope.sql()
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        SELECT trim(b.batch_no) AS batch_no,b.category,b.channel,
                          a.name AS app_name,b.creator,b.dept,b.content,b.status,
                          b.deferred_reason,trim(original.batch_no) AS resend_of,
                          b.is_test,b.segments,b.quota_cost,b.total,
                          b.removed_freq AS removed_freq_limit,b.delivered,b.failed,
                          b.unknown_cnt AS unknown,b.scheduled_at,b.created_at
                        FROM sms_batch b
                        LEFT JOIN app a ON a.id=b.app_id
                        LEFT JOIN sms_batch original ON original.id=b.resend_of
                        WHERE b.batch_no=:batch_no AND {predicate}
                        """
                    ),
                    {"batch_no": batch_no, **scope_params},
                )
                row = result.mappings().first()
                if row is None:
                    raise BatchNotFound
                return dict(row)
        finally:
            await engine.dispose()

    async def list_details(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        *,
        status: str | None,
        page: int,
        size: int,
    ) -> dict[str, object]:
        predicate, scope_params = scope.sql()
        status_predicate = "" if status is None else "AND m.status=:status"
        params: dict[str, object] = {
            "batch_no": batch_no,
            "offset": (page - 1) * size,
            "size": size,
            **scope_params,
        }
        if status is not None:
            params["status"] = status
        source = f"""
            FROM sms_message m
            JOIN sms_batch b ON b.id=m.batch_id
            LEFT JOIN sms_chunk c ON c.id=m.chunk_id
            WHERE b.batch_no=:batch_no AND {predicate} {status_predicate}
        """
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.scalar(text(f"SELECT count(*) {source}"), params)
                result = await connection.execute(
                    text(
                        f"""
                        SELECT m.id,m.phone_mask AS phone,m.status,c.vendor_task_id,
                          m.report_desc,m.report_time
                        {source}
                        ORDER BY m.id
                        OFFSET :offset LIMIT :size
                        """
                    ),
                    params,
                )
                return {
                    "total": int(count or 0),
                    "items": [dict(row) for row in result.mappings()],
                }
        finally:
            await engine.dispose()
