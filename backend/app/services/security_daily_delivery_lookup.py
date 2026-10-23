"""按报告或请求身份读取安全日报投递请求。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text

from app.services.security_daily import (
    DeliveryAction,
    DeliveryRequestState,
    SecurityDailyDeliveryRequest,
)


def _delivery_request(
    row: Any,
    *,
    delivery_id: str,
    error: str | None = None,
) -> SecurityDailyDeliveryRequest:
    return SecurityDailyDeliveryRequest(
        request_id=UUID(str(row["request_id"])),
        report_date=row["report_date"],
        action=cast(DeliveryAction, str(row["action"])),
        state=cast(DeliveryRequestState, str(row["state"])),
        requested_at=row["requested_at"],
        idempotent=True,
        config_version=int(row["config_version"]),
        delivery_id=delivery_id,
        delivery_generation=int(row["delivery_generation"]),
        recipient_set_digest=str(row["recipient_set_digest"]),
        error=error,
    )


async def fetch_latest_delivery_request(
    open_engine: Callable[[], Any], report_id: int
) -> SecurityDailyDeliveryRequest | None:
    """读取某条日报最近一次投递请求。"""

    engine = open_engine()
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT request_id,report_date,action,state,requested_at,
                           config_version,
                           COALESCE(delivery_generation,1) AS delivery_generation,
                           COALESCE(recipient_set_digest,'') AS recipient_set_digest
                    FROM security_daily_delivery_request
                    WHERE report_id=:report_id
                    ORDER BY requested_at DESC LIMIT 1
                    """
                ),
                {"report_id": report_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return _delivery_request(row, delivery_id=str(report_id))
    finally:
        await engine.dispose()


async def fetch_delivery_request(
    open_engine: Callable[[], Any], request_id: UUID
) -> SecurityDailyDeliveryRequest | None:
    """按 request_id 读取原投递请求，人工报告和自动报告共用这一入口。"""

    engine = open_engine()
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT request_id,report_id,report_date,action,state,requested_at,
                           config_version,
                           COALESCE(delivery_generation,1) AS delivery_generation,
                           COALESCE(recipient_set_digest,'') AS recipient_set_digest,
                           error
                    FROM security_daily_delivery_request
                    WHERE request_id=:request_id
                    """
                ),
                {"request_id": request_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            error = row["error"]
            return _delivery_request(
                row,
                delivery_id=str(row["report_id"]),
                error=None if error is None else str(error),
            )
    finally:
        await engine.dispose()
