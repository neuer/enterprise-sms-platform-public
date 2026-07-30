"""厂商余额快照与低余额告警用例。"""

from __future__ import annotations

from typing import Any, Protocol


class BalanceRepository(Protocol):
    async def alert_threshold(self) -> int: ...
    async def save_snapshot(self, balance: int) -> None: ...


class BalanceVendor(Protocol):
    async def get_balance(self) -> int: ...


class BalanceAlertSink(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
    ) -> None: ...


class BalanceMonitor:
    def __init__(
        self,
        repository: BalanceRepository,
        vendor: BalanceVendor,
        alerts: BalanceAlertSink,
    ) -> None:
        self.repository = repository
        self.vendor = vendor
        self.alerts = alerts

    async def poll(self) -> int:
        threshold = await self.repository.alert_threshold()
        if threshold < 0:
            raise ValueError("balance alert threshold must be non-negative")
        balance = await self.vendor.get_balance()
        if balance < 0:
            raise ValueError("vendor balance must be non-negative")
        await self.repository.save_snapshot(balance)
        if balance < threshold:
            await self.alerts.emit(
                alert_type="balance_low",
                level="warn",
                title="短信厂商余额低于阈值",
                detail={"balance": balance, "threshold": threshold},
                dedup_key="balance_low",
            )
        return 1
