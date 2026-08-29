"""批次与消息明细的最小权限查询服务。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.content_protection import decrypt_batch_display_content
from app.services.crypto import CryptoService
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

    def __init__(
        self,
        settings: Settings | None = None,
        crypto: CryptoService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.crypto = crypto

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    def _batch(self, row: Any) -> dict[str, object]:
        values = dict(row)
        batch_no = str(values["batch_no"])
        values["content"] = decrypt_batch_display_content(
            self.crypto,
            values.pop("display_content_enc"),
            batch_no,
        )
        return values

    @staticmethod
    def _message_status_counts_join() -> str:
        """按批次一次聚合未持久化在 sms_batch 上的消息状态计数。"""

        return """
            LEFT JOIN LATERAL (
              SELECT
                CAST(count(*) FILTER (WHERE m.status='pending') AS integer) AS pending,
                CAST(count(*) FILTER (WHERE m.status='sent') AS integer) AS sent,
                CAST(count(*) FILTER (WHERE m.status='other') AS integer) AS other
              FROM sms_message m WHERE m.batch_id=b.id
            ) message_counts ON TRUE
        """

    async def list_batches(
        self,
        *,
        scope: BatchAccessScope,
        category: str | None,
        statuses: Sequence[str] | None,
        channel: str | None,
        app_id: int | None,
        is_test: bool | None,
        batch_no: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        size: int,
    ) -> dict[str, object]:
        """按权限与运营筛选分页；只返回批次级无手机号字段。

        status_counts 为分面计数：与列表同过滤但不含状态条件，
        供前端状态分组 chips 在任意状态筛选下都显示完整分布。
        """

        if page < 1 or size < 1:
            raise ValueError("page and size must be positive")
        for moment in (start, end):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError("query time must include timezone")
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be later than end")
        predicate, scope_params = scope.sql()
        base_clauses = [predicate]
        base_params: dict[str, object] = {**scope_params}
        filters: tuple[tuple[str, object | None, str], ...] = (
            ("category", category, "b.category=:category"),
            ("channel", channel, "b.channel=:channel"),
            ("app_id", app_id, "b.app_id=:app_id"),
            ("is_test", is_test, "b.is_test=:is_test"),
            ("start", start, "b.created_at>=:start"),
            ("end", end, "b.created_at<=:end"),
        )
        for name, value, clause in filters:
            if value is not None:
                base_clauses.append(clause)
                base_params[name] = value
        if batch_no is not None and batch_no.strip():
            base_clauses.append("trim(b.batch_no) ILIKE :batch_no")
            base_params["batch_no"] = f"%{_escape_like(batch_no.strip())}%"
        clauses = list(base_clauses)
        params: dict[str, object] = {
            "limit": size,
            "offset": (page - 1) * size,
            **base_params,
        }
        if statuses:
            clauses.append("b.status=ANY(:statuses)")
            params["statuses"] = list(statuses)
        where = "\n AND ".join(clauses)
        base_where = "\n AND ".join(base_clauses)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count_result = await connection.execute(
                    text("SELECT count(*) FROM sms_batch b WHERE " + where),
                    params,
                )
                counts_result = await connection.execute(
                    text(
                        "SELECT b.status AS status,count(*) AS n"
                        " FROM sms_batch b WHERE " + base_where + " GROUP BY b.status"
                    ),
                    base_params,
                )
                rows_result = await connection.execute(
                    text(
                        f"""
                        SELECT trim(b.batch_no) AS batch_no,b.category,b.channel,
                          a.name AS app_name,b.creator,b.dept,b.display_content_enc,b.status,
                          b.deferred_reason,trim(original.batch_no) AS resend_of,
                          b.is_test,b.segments,b.quota_cost,b.total,
                          b.removed_freq AS removed_freq_limit,b.delivered,b.failed,
                          b.unknown_cnt AS unknown,message_counts.pending,
                          message_counts.sent,message_counts.other,
                          b.scheduled_at,b.created_at
                        FROM sms_batch b LEFT JOIN app a ON a.id=b.app_id
                        LEFT JOIN sms_batch original ON original.id=b.resend_of
                        {self._message_status_counts_join()}
                        WHERE """
                        + where
                        + " ORDER BY b.created_at DESC,b.id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                return {
                    "total": int(count_result.scalar_one()),
                    "status_counts": {
                        str(row["status"]): int(row["n"])
                        for row in counts_result.mappings()
                    },
                    "items": [self._batch(row) for row in rows_result.mappings()],
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
                          a.name AS app_name,b.creator,b.dept,b.display_content_enc,b.status,
                          b.deferred_reason,trim(original.batch_no) AS resend_of,
                          b.is_test,b.segments,b.quota_cost,b.total,
                          b.removed_freq AS removed_freq_limit,b.delivered,b.failed,
                          b.unknown_cnt AS unknown,message_counts.pending,
                          message_counts.sent,message_counts.other,
                          b.scheduled_at,b.created_at
                        FROM sms_batch b
                        LEFT JOIN app a ON a.id=b.app_id
                        LEFT JOIN sms_batch original ON original.id=b.resend_of
                        {self._message_status_counts_join()}
                        WHERE b.batch_no=:batch_no AND {predicate}
                        """
                    ),
                    {"batch_no": batch_no, **scope_params},
                )
                row = result.mappings().first()
                if row is None:
                    raise BatchNotFound
                return self._batch(row)
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
