"""仅供 migrate/sms_owner 维护消息与回复月分区。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import DDL, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.runtime_resources import bind_connection_system_audit
from app.settings import get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
PARTITION_PARENTS = ("sms_message", "sms_reply")
PARTITION_NAME = re.compile(r"^(sms_message|sms_reply)_(\d{4})_(0[1-9]|1[0-2])$")
PARTITION_BOUND = re.compile(
    r"^FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)$"
)
PARTITION_LOCK_KEY = 7_318_612_406_017_390_923


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    parent: str
    name: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class PartitionPlan:
    create: tuple[PartitionSpec, ...]
    drop_before: datetime


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    ensured: int
    dropped: int
    skipped: bool
    dry_run: bool


def _month(moment: datetime) -> datetime:
    local = moment.astimezone(SHANGHAI)
    return datetime(local.year, local.month, 1, tzinfo=SHANGHAI)


def _add_months(moment: datetime, months: int) -> datetime:
    absolute = moment.year * 12 + moment.month - 1 + months
    year, zero_month = divmod(absolute, 12)
    return datetime(year, zero_month + 1, 1, tzinfo=SHANGHAI)


def partition_start(name: str) -> datetime:
    """只解析两个固定父表的规范分区名。"""

    match = PARTITION_NAME.fullmatch(name)
    if match is None:
        raise ValueError("partition name is not owned by lifecycle maintenance")
    return datetime(int(match.group(2)), int(match.group(3)), 1, tzinfo=SHANGHAI)


def partition_plan(
    now: datetime,
    *,
    retention_months: int,
    future_months: int = 13,
) -> PartitionPlan:
    """生成上海自然月的固定分区窗口，不接受任意标识符。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("partition clock must include timezone")
    if retention_months < 1 or future_months < 1:
        raise ValueError("partition windows must be positive")
    anchor = _month(now)
    specs = tuple(
        PartitionSpec(
            parent,
            f"{parent}_{start:%Y_%m}",
            start,
            _add_months(start, 1),
        )
        for parent in PARTITION_PARENTS
        for start in (_add_months(anchor, offset) for offset in range(future_months + 1))
    )
    return PartitionPlan(specs, _add_months(anchor, -retention_months))


def _timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _identifier(value: str) -> str:
    if PARTITION_NAME.fullmatch(value) is None and value not in PARTITION_PARENTS:
        raise ValueError("partition identifier is not owned by lifecycle maintenance")
    return f'"{value}"'


def _qualified(value: str) -> str:
    return f'"public".{_identifier(value)}'


def _validate_partition_bound(name: str, value: str) -> None:
    match = PARTITION_BOUND.fullmatch(value)
    if match is None:
        raise ValueError("attached partition bound is unsafe")
    try:
        start = datetime.fromisoformat(match.group(1))
        end = datetime.fromisoformat(match.group(2))
    except ValueError as error:
        raise ValueError("attached partition bound is unsafe") from error
    expected_start = partition_start(name)
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or start.astimezone(SHANGHAI) != expected_start
        or end.astimezone(SHANGHAI) != _add_months(expected_start, 1)
    ):
        raise ValueError("attached partition bound does not match its name")


async def maintain(
    connection: Any,
    *,
    dry_run: bool = False,
    future_months: int = 13,
) -> MaintenanceResult:
    """在 owner 事务中持锁维护未来窗口和保留边界。"""

    if not 3 <= future_months <= 24:
        raise ValueError("future partition window must be between 3 and 24 months")
    lock_result = await connection.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": PARTITION_LOCK_KEY},
    )
    if lock_result.scalar_one() is not True:
        return MaintenanceResult(0, 0, True, dry_run)

    retention_result = await connection.execute(
        text("SELECT value FROM sys_config WHERE key='msg_retention_months'")
    )
    try:
        retention = int(retention_result.scalar_one_or_none() or 12)
    except (TypeError, ValueError) as error:
        raise ValueError("message retention configuration is invalid") from error
    if not 1 <= retention <= 120:
        raise ValueError("message retention configuration is outside the safe range")
    now_result = await connection.execute(text("SELECT now()"))
    plan = partition_plan(
        now_result.scalar_one(),
        retention_months=retention,
        future_months=future_months,
    )
    existing_result = await connection.execute(
        text(
            """
            SELECT parent.relname AS parent_name, child.relname AS child_name,
                   pg_get_expr(child.relpartbound, child.oid) AS partition_bound
            FROM pg_inherits
            JOIN pg_class parent ON parent.oid=inhparent
            JOIN pg_class child ON child.oid=inhrelid
            JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
            JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
            WHERE parent.relname=ANY(CAST(:parents AS text[]))
              AND parent_ns.nspname='public'
              AND child_ns.nspname='public'
            """
        ),
        {"parents": list(PARTITION_PARENTS)},
    )
    existing: dict[str, str] = {}
    for row in existing_result.mappings():
        parent = str(row["parent_name"])
        name = str(row["child_name"])
        match = PARTITION_NAME.fullmatch(name)
        if parent not in PARTITION_PARENTS or match is None or match.group(1) != parent:
            raise ValueError("attached partition identity is unsafe")
        _validate_partition_bound(name, str(row["partition_bound"]))
        existing[name] = parent

    missing = tuple(spec for spec in plan.create if spec.name not in existing)
    expired = sorted(
        name
        for name in existing
        if partition_start(name) < plan.drop_before
    )
    if not dry_run:
        for spec in missing:
            await connection.execute(
                DDL(  # type: ignore[no-untyped-call]
                    f"CREATE TABLE {_qualified(spec.name)} "
                    f"PARTITION OF {_qualified(spec.parent)} FOR VALUES "
                    f"FROM ('{_timestamp(spec.start)}') TO ('{_timestamp(spec.end)}')"
                )
            )
        for name in expired:
            await connection.execute(
                DDL(  # type: ignore[no-untyped-call]
                    f"DROP TABLE {_qualified(name)}"
                )
            )
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(
                  actor,actor_subject_kind,action,object_type,object_id,after_val
                )
                VALUES (
                  'partition-maintenance','system','partition.maintenance',
                  'partition_window','scheduled',CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "payload": json.dumps(
                    {
                        "dropped": len(expired),
                        "ensured": len(missing),
                        "future_months": future_months,
                        "retention_months": retention,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            },
        )
    return MaintenanceResult(len(missing), len(expired), False, dry_run)


async def _run_once(
    *,
    dry_run: bool,
    future_months: int,
) -> MaintenanceResult:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_owner_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        async with engine.begin() as connection:
            # migrate 是 sms_owner；显式绑定受认证的 system 上下文后再写审计。
            # owner 由数据库单独授权，api 仅用于选择隔离的签名域格式。
            await bind_connection_system_audit(
                connection,
                actor_name="partition-maintenance",
                action="partition.maintenance",
                producer_domain="api",
            )
            return await maintain(
                connection,
                dry_run=dry_run,
                future_months=future_months,
            )
    finally:
        await engine.dispose()


def _execute_with_retry(
    *,
    dry_run: bool,
    future_months: int,
    max_attempts: int,
    sleeper: Callable[[float], None] = time.sleep,
) -> MaintenanceResult:
    if not 1 <= max_attempts <= 5:
        raise ValueError("partition maintenance attempts must be between 1 and 5")
    for attempt in range(1, max_attempts + 1):
        try:
            return asyncio.run(
                _run_once(
                    dry_run=dry_run,
                    future_months=future_months,
                )
            )
        except Exception:
            if attempt == max_attempts:
                raise
            sleeper(float(2 ** (attempt - 1)))
    raise AssertionError("partition maintenance retry loop did not return")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--future-months", type=int, default=13)
    parser.add_argument("--max-attempts", type=int, default=3)
    arguments = parser.parse_args(argv)
    try:
        result = _execute_with_retry(
            dry_run=arguments.dry_run,
            future_months=arguments.future_months,
            max_attempts=arguments.max_attempts,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "lifecycle_alert",
                    "error_type": type(error).__name__,
                    "operation": "partition-maintenance",
                    "status": "failed",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(
        "partition maintenance complete: "
        f"ensured={result.ensured} dropped={result.dropped} "
        f"dry_run={str(result.dry_run).lower()} "
        f"skipped={str(result.skipped).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
