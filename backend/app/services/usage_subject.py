"""发送账本主体：调用主体、计费主体与审计主体分离。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

SYSTEM_UNCERTAIN_RESEND_APP_NAME = "system-uncertain-resend"
UsageSubjectKind = Literal["api_app", "system_effect"]


@dataclass(frozen=True, slots=True)
class UsageSubject:
    """计费/配额主体；只能由已锁定的 uncertain effect 或真实 API app 构造。"""

    kind: UsageSubjectKind
    app_id: int
    dept: str
    category: str
    account_id: int | None = None
    resolution_id: int | None = None
    effect_generation: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"api_app", "system_effect"}:
            raise ValueError("usage subject kind invalid")
        if self.app_id < 1:
            raise ValueError("usage subject app_id must be a positive id")
        dept = self.dept.strip()
        if not dept or len(dept) > 128:
            raise ValueError("usage subject dept required")
        if self.category not in {"verify", "notice", "market"}:
            raise ValueError("usage subject category invalid")
        if self.kind == "system_effect" and (
            self.resolution_id is None
            or self.resolution_id < 1
            or self.effect_generation is None
            or self.effect_generation < 1
        ):
            raise ValueError("system usage subject requires resolution")
        object.__setattr__(self, "dept", dept)

    def fingerprint(self) -> dict[str, object]:
        """进入幂等指纹的无 PII 计费主体摘要。"""

        return {
            "kind": self.kind,
            "app_id": self.app_id,
            "dept": self.dept,
            "category": self.category,
            "resolution_id": self.resolution_id,
            "effect_generation": self.effect_generation,
        }


@dataclass(frozen=True, slots=True)
class SystemUncertainApp:
    app_id: int
    name: str
    daily_quota: int
    allowed_categories: frozenset[str]
    max_in_flight_chunks: int
    rate_limit_per_min: int
    blacklist_check: bool


@dataclass(frozen=True, slots=True)
class UncertainResendContext:
    """仅 Outbox worker 在锁定已批准 resolution 后可构造的内部重发上下文。"""

    resolution_id: int
    effect_generation: int
    proposer_account_id: int
    confirmer_account_id: int
    source_batch_id: int
    source_chunk_id: int
    source_channel: str
    source_app_id: int | None
    source_dept: str
    source_category: str
    usage_subject: UsageSubject


async def load_system_uncertain_resend_app(
    connection: AsyncConnection,
) -> SystemUncertainApp:
    """读取受控内部 system app；缺失或停用不得猜测另一个主体。"""

    row = (
        await connection.execute(
            text(
                """
                SELECT id,name,daily_quota,allowed_categories,
                       max_in_flight_chunks,rate_limit_per_min,blacklist_check
                FROM app
                WHERE name=:name
                  AND usage_subject_kind='system_effect'
                  AND status=1
                """
            ),
            {"name": SYSTEM_UNCERTAIN_RESEND_APP_NAME},
        )
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("system usage subject unavailable")
    categories = frozenset(
        item.strip()
        for item in str(row["allowed_categories"]).split(",")
        if item.strip()
    )
    daily_quota = int(row["daily_quota"])
    if int(row["id"]) < 1 or daily_quota < 1 or not categories:
        raise LookupError("system usage subject unavailable")
    return SystemUncertainApp(
        int(row["id"]),
        str(row["name"]),
        daily_quota,
        categories,
        int(row["max_in_flight_chunks"]),
        int(row["rate_limit_per_min"]),
        bool(row["blacklist_check"]),
    )


def require_source_dept(value: Any) -> str:
    """源批次部门必须可恢复；禁止把固定字符串 web 当作缺省部门。"""

    dept = str(value or "").strip()
    if not dept or len(dept) > 128:
        raise ValueError("source dept unavailable")
    return dept
