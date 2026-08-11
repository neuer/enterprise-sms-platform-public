from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

import app.services.import_repository as repository_module
from app.core.auth.accounts import SecurityPrincipal
from app.services.import_repository import ImportParseClaim, SqlImportRepository
from app.services.imports import ImportPhone


class FakeResult:
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        *,
        scalar: object = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def scalars(self) -> Iterator[object]:
        return iter(row["value"] for row in self.rows)


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository(results: list[FakeResult]) -> tuple[SqlImportRepository, FakeConnection]:
    connection = FakeConnection(results)
    instance = SqlImportRepository()
    instance._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return instance, connection


def principal() -> SecurityPrincipal:
    return SecurityPrincipal(11, 101, "operator01", "业务一部", "operator")


@pytest.mark.asyncio
async def test_registration_records_deterministic_source_before_file_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = UUID("11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(repository_module, "uuid4", lambda: import_id)
    instance, connection = repository(
        [
            FakeResult(
                [
                    {
                        "import_id": import_id,
                        "expires_at": datetime(2026, 7, 29, tzinfo=UTC),
                    }
                ]
            )
        ]
    )

    stored = await instance.register(
        principal=principal(),
        filename="phones.csv",
        source_size=12,
        expire_hours=24,
    )

    sql, params = connection.calls[0]
    assert "'staging'" in sql
    assert "source_file,source_size,parse_status" in sql
    assert params["source_file"] == f"import-{import_id}.smsx"
    assert params["filename"] == "upload.csv"
    assert stored.status == "staging"
    assert stored.source_file == f"import-{import_id}.smsx"


@pytest.mark.asyncio
async def test_expired_third_attempt_is_failed_before_dispatch_scan() -> None:
    instance, connection = repository(
        [FakeResult(), FakeResult([{"value": "import-ready"}])]
    )

    assert await instance.pending_parse_ids() == ["import-ready"]

    exhaustion_sql = connection.calls[0][0]
    scan_sql = connection.calls[1][0]
    assert "parse_attempts>=3" in exhaustion_sql
    assert "parse_status='failed'" in exhaustion_sql
    assert "parse_attempts<3" in scan_sql
    assert "parse_lease_expires_at<=now()" in scan_sql


@pytest.mark.asyncio
async def test_parse_batch_write_is_fenced_and_renews_lease() -> None:
    claim = ImportParseClaim(
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
        "phones.csv",
        "import.smsx",
        12,
    )
    instance, connection = repository([FakeResult(scalar=7), FakeResult()])
    phone = ImportPhone(b"cipher", "a" * 64, "138****8000", 1, 2)

    assert await instance.append_parse_batch(claim, (phone,)) is True

    renew_sql = connection.calls[0][0]
    insert_sql = connection.calls[1][0]
    assert "parse_lease_id=:lease_id" in renew_sql
    assert "parse_lease_expires_at>now()" in renew_sql
    assert "ON CONFLICT(import_task_id,phone_hmac) DO NOTHING" in insert_sql
    assert connection.calls[1][1][0]["phone_enc"] == b"cipher"


@pytest.mark.asyncio
async def test_permanent_parse_failure_deletes_partial_protected_rows() -> None:
    claim = ImportParseClaim(
        UUID("44444444-4444-4444-8444-444444444444"),
        UUID("55555555-5555-4555-8555-555555555555"),
        "phones.csv",
        "import.smsx",
        12,
    )
    instance, connection = repository([FakeResult(scalar=9), FakeResult()])

    assert await instance.fail_parse(claim, "IMPORT_PARSE_FAILED") is True

    assert "RETURNING id" in connection.calls[0][0]
    assert "DELETE FROM import_phone" in connection.calls[1][0]
    assert connection.calls[1][1] == {"task_id": 9}
