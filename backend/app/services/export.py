"""异步导出的权限、过滤器规范化与行数上限。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from app.core.auth.accounts import SecurityPrincipal
from app.services.crypto import CryptoService

MAX_EXPORT_ROWS = 100_000
DECRYPTED_EXPORT_ROLES = {"approver", "admin"}


class ExportForbidden(PermissionError):
    pass


class ExportTooLarge(ValueError):
    pass


class ExportNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ExportRequestFilters:
    start: datetime | None = None
    end: datetime | None = None
    category: str | None = None
    status: str | None = None
    app_id: int | None = None
    dept: str | None = None
    batch_no: str | None = None
    phone: str | None = None
    dataset: Literal["message", "unmatched"] = "message"


@dataclass(frozen=True, slots=True)
class ExportFilterSet:
    start: datetime | None
    end: datetime | None
    category: str | None
    status: str | None
    app_id: int | None
    batch_no: str | None
    phone_hmacs: tuple[str, ...]
    scope_dept: str | None
    dataset: Literal["message", "unmatched"] = "message"

    def safe_json(self) -> dict[str, object]:
        """可持久化过滤器；只含索引和部门 scope，不含手机号。"""

        return {
            "dataset": self.dataset,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "category": self.category,
            "status": self.status,
            "app_id": self.app_id,
            "batch_no": self.batch_no,
            "phone_hmacs": list(self.phone_hmacs),
            "scope_dept": self.scope_dept,
        }

    @classmethod
    def from_safe_json(cls, value: dict[str, Any]) -> ExportFilterSet:
        def moment(key: str) -> datetime | None:
            raw = value.get(key)
            return datetime.fromisoformat(str(raw)) if raw is not None else None

        raw_hmacs = value.get("phone_hmacs") or []
        if not isinstance(raw_hmacs, list):
            raise ValueError("invalid persisted export phone_hmacs")
        return cls(
            start=moment("start"),
            end=moment("end"),
            category=str(value["category"]) if value.get("category") is not None else None,
            status=str(value["status"]) if value.get("status") is not None else None,
            app_id=int(value["app_id"]) if value.get("app_id") is not None else None,
            batch_no=str(value["batch_no"]) if value.get("batch_no") is not None else None,
            phone_hmacs=tuple(str(item) for item in raw_hmacs),
            scope_dept=(str(value["scope_dept"]) if value.get("scope_dept") is not None else None),
            dataset=("unmatched" if value.get("dataset") == "unmatched" else "message"),
        )


@dataclass(frozen=True, slots=True)
class ExportTaskInfo:
    id: int
    public_id: UUID
    status: str
    decrypted: bool
    row_count: int | None
    file_path: str | None
    expires_at: datetime | None
    created_at: datetime


class ExportRepository(Protocol):
    async def count_rows(self, filters: ExportFilterSet) -> int: ...

    async def create(
        self,
        *,
        principal: SecurityPrincipal,
        filters: ExportFilterSet,
        decrypted: bool,
    ) -> ExportTaskInfo: ...

    async def get_accessible(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        retention_days: int,
    ) -> ExportTaskInfo | None: ...

    async def get_downloadable_and_audit(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        ip: str,
        retention_days: int,
    ) -> ExportTaskInfo | None: ...


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    for moment in (start, end):
        if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
            raise ValueError("export time must include timezone")
    if start is not None and end is not None and start > end:
        raise ValueError("export start must not be later than end")


class ExportService:
    """在创建任务前固定权限 scope，并把手机号立即不可逆转换为 HMAC。"""

    def __init__(
        self,
        repository: ExportRepository,
        crypto: CryptoService,
        *,
        retention_days: int,
    ) -> None:
        if retention_days < 1:
            raise ValueError("export retention_days must be positive")
        self.repository = repository
        self.crypto = crypto
        self.retention_days = retention_days

    def _normalize(
        self,
        filters: ExportRequestFilters,
        *,
        role: str,
        dept: str,
    ) -> ExportFilterSet:
        _validate_range(filters.start, filters.end)
        if role != "admin" and filters.dept is not None and filters.dept != dept:
            raise ExportForbidden("不能导出其他部门数据")
        scope_dept = filters.dept if role == "admin" else dept
        phone_hmacs = (
            tuple(self.crypto.hmac_candidates(filters.phone).values())
            if filters.phone is not None
            else ()
        )
        return ExportFilterSet(
            filters.start,
            filters.end,
            filters.category,
            filters.status,
            filters.app_id,
            filters.batch_no,
            phone_hmacs,
            scope_dept,
            filters.dataset,
        )

    async def create(
        self,
        filters: ExportRequestFilters,
        *,
        decrypted: bool,
        principal: SecurityPrincipal,
    ) -> ExportTaskInfo:
        if filters.dataset == "unmatched" and principal.role != "admin":
            raise ExportForbidden("仅管理员可导出无主报告")
        if decrypted and principal.role not in DECRYPTED_EXPORT_ROLES:
            raise ExportForbidden("当前角色无明文导出权限")
        normalized = self._normalize(
            filters,
            role=principal.role,
            dept=principal.dept,
        )
        if await self.repository.count_rows(normalized) > MAX_EXPORT_ROWS:
            raise ExportTooLarge("导出结果超过 100000 行")
        return await self.repository.create(
            principal=principal,
            filters=normalized,
            decrypted=decrypted,
        )

    async def get(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
    ) -> ExportTaskInfo:
        task = await self.repository.get_accessible(
            public_id,
            principal=principal,
            retention_days=self.retention_days,
        )
        if task is None:
            raise ExportNotFound("导出任务不存在")
        return task

    async def get_downloadable(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> ExportTaskInfo:
        """在同一数据库事务内重验下载权限、有效期并写无 PII 审计。"""

        task = await self.repository.get_downloadable_and_audit(
            public_id,
            principal=principal,
            ip=ip,
            retention_days=self.retention_days,
        )
        if task is None:
            raise ExportNotFound("导出文件不存在或已过期")
        return task
