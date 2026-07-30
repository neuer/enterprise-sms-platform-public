from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts_support.maintain_partitions import (
    PARTITION_PARENTS,
    MaintenanceResult,
    _execute_with_retry,
    maintain,
    partition_plan,
    partition_start,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_partition_plan_uses_shanghai_months_and_thirteen_month_buffer() -> None:
    now = datetime(2026, 7, 31, 23, 59, tzinfo=SHANGHAI)

    plan = partition_plan(now, retention_months=12)

    assert len(plan.create) == len(PARTITION_PARENTS) * 14
    assert plan.create[0].name == "sms_message_2026_07"
    assert plan.create[0].start == datetime(2026, 7, 1, tzinfo=SHANGHAI)
    assert plan.create[0].end == datetime(2026, 8, 1, tzinfo=SHANGHAI)
    assert plan.create[-1].name == "sms_reply_2027_08"
    assert plan.drop_before == datetime(2025, 7, 1, tzinfo=SHANGHAI)


def test_partition_start_rejects_unowned_or_malformed_names() -> None:
    assert partition_start("sms_reply_2025_06") == datetime(2025, 6, 1, tzinfo=SHANGHAI)
    with pytest.raises(ValueError, match="partition"):
        partition_start("audit_log_2025_06")
    with pytest.raises(ValueError, match="partition"):
        partition_start('sms_reply_2025_06";DROP TABLE audit_log;--')


class FakeResult:
    def __init__(
        self,
        *,
        scalar: object | None = None,
        rows: list[dict[str, str]] | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object | None:
        return self.scalar

    def mappings(self) -> list[dict[str, str]]:
        return self.rows


class FakeConnection:
    def __init__(self, *, lock_acquired: bool = True) -> None:
        self.lock_acquired = lock_acquired
        self.statements: list[tuple[str, object | None]] = []

    async def execute(
        self,
        statement: object,
        parameters: object | None = None,
    ) -> FakeResult:
        sql = str(statement)
        self.statements.append((sql, parameters))
        if "pg_try_advisory_xact_lock" in sql:
            return FakeResult(scalar=self.lock_acquired)
        if "msg_retention_months" in sql:
            return FakeResult(scalar="12")
        if "SELECT now()" in sql:
            return FakeResult(scalar=datetime(2026, 7, 28, 8, tzinfo=SHANGHAI))
        if "FROM pg_inherits" in sql:
            return FakeResult(
                rows=[
                    {
                        "parent_name": "sms_message",
                        "child_name": "sms_message_2026_07",
                        "partition_bound": (
                            "FOR VALUES FROM ('2026-07-01 00:00:00+08:00') "
                            "TO ('2026-08-01 00:00:00+08:00')"
                        ),
                    },
                    {
                        "parent_name": "sms_message",
                        "child_name": "sms_message_2025_06",
                        "partition_bound": (
                            "FOR VALUES FROM ('2025-06-01 00:00:00+08:00') "
                            "TO ('2025-07-01 00:00:00+08:00')"
                        ),
                    },
                    {
                        "parent_name": "sms_reply",
                        "child_name": "sms_reply_2025_06",
                        "partition_bound": (
                            "FOR VALUES FROM ('2025-06-01 00:00:00+08:00') "
                            "TO ('2025-07-01 00:00:00+08:00')"
                        ),
                    },
                ]
            )
        return FakeResult()


@pytest.mark.asyncio
async def test_concurrent_partition_maintenance_skips_without_ddl() -> None:
    connection = FakeConnection(lock_acquired=False)

    result = await maintain(connection, future_months=3)

    assert result.skipped is True
    assert len(connection.statements) == 1


@pytest.mark.asyncio
async def test_partition_dry_run_plans_without_ddl_or_audit() -> None:
    connection = FakeConnection()

    result = await maintain(connection, dry_run=True, future_months=3)

    assert result.ensured == 7
    assert result.dropped == 2
    assert result.dry_run is True
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "CREATE TABLE" not in sql
    assert "DROP TABLE" not in sql
    assert "INSERT INTO audit_log" not in sql


@pytest.mark.asyncio
async def test_partition_maintenance_executes_safe_plan_and_audits_counts() -> None:
    connection = FakeConnection()

    result = await maintain(connection, future_months=3)

    assert result.ensured == 7
    assert result.dropped == 2
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert sql.count('CREATE TABLE "public"') == 7
    assert sql.count("DROP TABLE") == 2
    assert 'DROP TABLE "public"."sms_message_2025_06"' in sql
    audit = next(
        parameters
        for statement, parameters in connection.statements
        if "INSERT INTO audit_log" in statement
    )
    assert isinstance(audit, dict)
    assert audit["payload"] == (
        '{"dropped":2,"ensured":7,"future_months":3,"retention_months":12}'
    )


@pytest.mark.asyncio
async def test_partition_maintenance_rejects_name_bound_mismatch_before_ddl() -> None:
    connection = FakeConnection()
    original_execute = connection.execute

    async def mismatched_execute(
        statement: object,
        parameters: object | None = None,
    ) -> FakeResult:
        result = await original_execute(statement, parameters)
        if "FROM pg_inherits" in str(statement):
            result.rows[0]["partition_bound"] = (
                "FOR VALUES FROM ('2026-08-01 00:00:00+08:00') "
                "TO ('2026-09-01 00:00:00+08:00')"
            )
        return result

    connection.execute = mismatched_execute  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="does not match"):
        await maintain(connection, future_months=3)

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "CREATE TABLE" not in sql
    assert "DROP TABLE" not in sql


def test_partition_maintenance_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    async def run_once(*, dry_run: bool, future_months: int) -> MaintenanceResult:
        nonlocal attempts
        attempts += 1
        assert dry_run is False
        assert future_months == 13
        if attempts < 3:
            raise RuntimeError("transient")
        return MaintenanceResult(1, 0, False, False)

    monkeypatch.setattr(
        "scripts_support.maintain_partitions._run_once",
        run_once,
    )
    result = _execute_with_retry(
        dry_run=False,
        future_months=13,
        max_attempts=3,
        sleeper=delays.append,
    )

    assert result.ensured == 1
    assert attempts == 3
    assert delays == [1.0, 2.0]
