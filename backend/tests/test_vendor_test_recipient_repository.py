from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)


class FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None


class FakeResult:
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        *,
        scalar: object | None = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeMappings:
        return FakeMappings(self.rows)

    def scalar_one(self) -> object:
        assert self.scalar is not None
        return self.scalar

    def scalar_one_or_none(self) -> object | None:
        return self.scalar


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []
        self.begin_calls = 0
        self.connect_calls = 0

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

    def connect(self) -> FakeContext:
        self.connection.connect_calls += 1
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        self.connection.begin_calls += 1
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository(results: list[FakeResult]):
    from app.services.vendor_test_recipient_repository import SqlVendorTestRecipientRepository

    connection = FakeConnection(results)
    repo = SqlVendorTestRecipientRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


def candidate():
    from app.services.vendor_test_recipient import VendorTestRecipientCreate

    return VendorTestRecipientCreate(
        label="值班测试机",
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
    )


@pytest.mark.asyncio
async def test_create_locks_checks_all_hmac_candidates_and_audits_without_phone_data() -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=False),
            FakeResult(scalar=False),
            FakeResult(
                [
                    {
                        "id": 7,
                        "status": "active",
                        "created_at": NOW,
                    }
                ]
            ),
            FakeResult(),
            FakeResult(),
        ]
    )
    candidates = {1: "b" * 64, 2: "a" * 64}

    record = await repo.create(candidate(), candidates, actor="admin")

    assert record.id == 7 and record.phone_mask == "138****8000"
    assert connection.calls[0][1] == {"lock_name": "vendor-test-lifecycle"}
    assert connection.calls[1][1] == {"lock_name": "vendor-test-recipient"}
    assert "operation_type='reset_configuration'" in connection.calls[2][0]
    duplicate_sql, duplicate_params = connection.calls[3]
    assert "hmac_digest" in duplicate_sql and "hmac_key_version" in duplicate_sql
    assert set(duplicate_params.values()) == {1, 2, "a" * 64, "b" * 64}
    insert_sql, insert_params = connection.calls[4]
    assert "INSERT INTO vendor_test_recipient" in insert_sql
    assert insert_params["phone_enc"] == b"ciphertext-only"
    alias_sql, alias_params = connection.calls[5]
    assert "INSERT INTO vendor_test_recipient_hmac_alias" in alias_sql
    assert set(alias_params.values()) == {7, 1, 2, "a" * 64, "b" * 64}
    audit_sql, audit_params = connection.calls[6]
    assert "INSERT INTO audit_log" in audit_sql
    assert json.loads(audit_params["after"]) == {"count": 1}
    rendered_audit = json.dumps(audit_params, default=str)
    assert "138****8000" not in rendered_audit
    assert "a" * 64 not in rendered_audit
    assert "ciphertext-only" not in rendered_audit


@pytest.mark.asyncio
async def test_create_rejects_duplicate_without_insert_or_sensitive_error() -> None:
    from app.services.vendor_test_recipient import DuplicateVendorTestRecipient

    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=False),
            FakeResult(scalar=True),
        ]
    )

    with pytest.raises(DuplicateVendorTestRecipient) as captured:
        await repo.create(candidate(), {2: "a" * 64}, actor="admin")

    assert len(connection.calls) == 4
    assert "a" * 64 not in str(captured.value)


@pytest.mark.asyncio
async def test_create_fails_closed_during_nonterminal_reset_without_insert_or_audit() -> None:
    from app.services.vendor_test_recipient import RecipientBusy

    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=True),
        ]
    )

    with pytest.raises(RecipientBusy):
        await repo.create(candidate(), {2: "a" * 64}, actor="admin")

    assert connection.calls[0][1] == {"lock_name": "vendor-test-lifecycle"}
    assert connection.calls[1][1] == {"lock_name": "vendor-test-recipient"}
    assert "operation_type='reset_configuration'" in connection.calls[2][0]
    rendered = repr(connection.calls)
    assert "INSERT INTO vendor_test_recipient(" not in rendered
    assert "INSERT INTO audit_log" not in rendered


@pytest.mark.asyncio
async def test_list_selects_mask_but_never_ciphertext_or_hmac() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "id": 7,
                        "label": "值班测试机",
                        "phone_mask": "138****8000",
                        "status": "active",
                        "created_at": NOW,
                        "disabled_at": None,
                    }
                ]
            )
        ]
    )

    rows = await repo.list_summaries(include_disabled=False)

    sql = connection.calls[0][0]
    assert "phone_mask" in sql
    assert "phone_enc" not in sql and "phone_hmac" not in sql
    assert "status='active'" in sql
    assert rows[0].phone_mask == "138****8000"


@pytest.mark.asyncio
async def test_resolve_for_send_reads_only_internal_protected_tuple() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "id": 7,
                        "phone_enc": b"ciphertext-only",
                        "phone_hmac": "a" * 64,
                        "phone_mask": "138****8000",
                        "key_version": 2,
                    }
                ]
            ),
            FakeResult(
                [
                    {"key_version": 1, "phone_hmac": "b" * 64},
                    {"key_version": 2, "phone_hmac": "a" * 64},
                ]
            ),
        ]
    )

    resolved = await repo.resolve_for_send(7)

    sql = connection.calls[0][0]
    assert all(field in sql for field in ("phone_enc", "phone_hmac", "phone_mask", "key_version"))
    assert "status='active'" in sql
    assert resolved.phone_enc == b"ciphertext-only"
    assert resolved.phone_hmac == "a" * 64
    assert resolved.phone_mask == "138****8000"
    assert resolved.hmac_candidates == ((1, "b" * 64), (2, "a" * 64))


@pytest.mark.asyncio
async def test_resolve_by_hmac_candidates_finds_only_active_recipient_without_plain_phone() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "id": 7,
                        "phone_enc": b"ciphertext-only",
                        "phone_hmac": "a" * 64,
                        "phone_mask": "138****8000",
                        "key_version": 2,
                    }
                ]
            ),
            FakeResult(
                [
                    {"key_version": 1, "phone_hmac": "b" * 64},
                    {"key_version": 2, "phone_hmac": "a" * 64},
                ]
            ),
        ]
    )

    resolved = await repo.resolve_by_hmac_candidates({1: "b" * 64, 2: "a" * 64})

    lookup_sql, lookup_params = connection.calls[0]
    assert "vendor_test_recipient_hmac_alias" in lookup_sql
    assert "status='active'" in lookup_sql
    assert "phone_enc" in lookup_sql and "phone_hmac" in lookup_sql
    assert set(lookup_params.values()) == {1, 2, "a" * 64, "b" * 64}
    assert "13800138000" not in str(connection.calls)
    assert resolved.id == 7
    assert resolved.phone_enc == b"ciphertext-only"
    assert resolved.hmac_candidates == ((1, "b" * 64), (2, "a" * 64))


@pytest.mark.asyncio
async def test_resolve_by_hmac_candidates_rejects_ambiguous_matches() -> None:
    from app.services.vendor_test_recipient import RecipientHmacIndexStale

    repo, _connection = repository(
        [
            FakeResult(
                [
                    {
                        "id": 7,
                        "phone_enc": b"ciphertext-one",
                        "phone_hmac": "a" * 64,
                        "phone_mask": "138****8000",
                        "key_version": 2,
                    },
                    {
                        "id": 8,
                        "phone_enc": b"ciphertext-two",
                        "phone_hmac": "c" * 64,
                        "phone_mask": "139****9000",
                        "key_version": 2,
                    },
                ]
            )
        ]
    )

    with pytest.raises(RecipientHmacIndexStale):
        await repo.resolve_by_hmac_candidates({1: "b" * 64, 2: "a" * 64})


@pytest.mark.asyncio
async def test_refresh_hmac_candidates_matches_existing_alias_and_replaces_atomically() -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(
                [
                    {
                        "id": 7,
                        "label": "值班测试机",
                        "phone_mask": "138****8000",
                        "status": "active",
                        "created_at": NOW,
                        "disabled_at": None,
                    }
                ]
            ),
            FakeResult(scalar=True),
            FakeResult(scalar=False),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )
    candidates = {1: "b" * 64, 2: "a" * 64}

    summary = await repo.refresh_hmac_candidates(7, candidates, actor="admin")

    assert summary.phone_mask == "138****8000"
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "FOR UPDATE" in connection.calls[1][0]
    assert "vendor_test_recipient_hmac_alias" in connection.calls[2][0]
    assert connection.calls[2][1] == {
        "recipient_id": 7,
        "candidate_version_0": 1,
        "candidate_hmac_0": "b" * 64,
        "candidate_version_1": 2,
        "candidate_hmac_1": "a" * 64,
    }
    assert "recipient_id<>:recipient_id" in connection.calls[3][0]
    assert "DELETE FROM vendor_test_recipient_hmac_alias" in connection.calls[4][0]
    assert "INSERT INTO vendor_test_recipient_hmac_alias" in connection.calls[5][0]
    audit_sql, audit_params = connection.calls[6]
    assert "INSERT INTO audit_log" in audit_sql
    assert audit_params["action"] == "vendor_test_recipient_refresh_index"
    assert json.loads(audit_params["after"]) == {"count": 1, "versions": 2}


@pytest.mark.asyncio
async def test_refresh_hmac_candidates_rejects_wrong_phone_without_mutation() -> None:
    from app.services.vendor_test_recipient import InvalidVendorTestRecipient

    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(
                [
                    {
                        "id": 7,
                        "label": "值班测试机",
                        "phone_mask": "138****8000",
                        "status": "active",
                        "created_at": NOW,
                        "disabled_at": None,
                    }
                ]
            ),
            FakeResult(scalar=False),
        ]
    )

    with pytest.raises(InvalidVendorTestRecipient):
        await repo.refresh_hmac_candidates(7, {2: "f" * 64}, actor="admin")

    assert len(connection.calls) == 3


@pytest.mark.asyncio
async def test_disable_fails_closed_when_any_uat_operation_is_active() -> None:
    from app.services.vendor_test_recipient import RecipientBusy

    repo, connection = repository(
        [
            FakeResult(),
            FakeResult([{"id": 7, "status": "active"}]),
            FakeResult(scalar=True),
        ]
    )

    with pytest.raises(RecipientBusy):
        await repo.disable(7, actor="admin")

    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    busy_sql = connection.calls[2][0]
    assert "vendor_test_operation" in busy_sql
    assert "sms_batch" in busy_sql
    assert "is_test" in busy_sql
    assert "queued" in busy_sql and "sending" in busy_sql
    assert len(connection.calls) == 3


@pytest.mark.asyncio
async def test_purge_all_fails_closed_before_delete_when_uat_is_active() -> None:
    from app.services.vendor_test_recipient import RecipientBusy

    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=True),
        ]
    )

    with pytest.raises(RecipientBusy):
        await repo.purge_all(actor="admin")

    assert connection.calls[0][1] == {"lock_name": "vendor-test-lifecycle"}
    assert connection.calls[1][1] == {"lock_name": "vendor-test-recipient"}
    assert "vendor_test_operation" in connection.calls[2][0]
    assert len(connection.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("deleted_ids", "expected_count"), [([7, 9], 2), ([], 0)])
async def test_purge_all_deletes_in_one_transaction_and_writes_one_count_only_audit(
    deleted_ids: list[int],
    expected_count: int,
) -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=False),
            FakeResult([{"id": item} for item in deleted_ids]),
            FakeResult(),
        ]
    )

    count = await repo.purge_all(actor="admin")

    assert count == expected_count
    assert connection.begin_calls == 1
    assert connection.connect_calls == 0
    assert connection.calls[0][1] == {"lock_name": "vendor-test-lifecycle"}
    assert connection.calls[1][1] == {"lock_name": "vendor-test-recipient"}
    delete_sql, delete_params = connection.calls[3]
    assert "DELETE FROM vendor_test_recipient" in delete_sql
    assert "RETURNING id" in delete_sql
    assert "vendor_test_recipient_hmac_alias" not in delete_sql
    assert delete_params is None
    audit_sql, audit_params = connection.calls[4]
    assert "INSERT INTO audit_log" in audit_sql
    assert audit_params == {
        "actor": "admin",
        "action": "vendor_test_recipient_purge_all",
        "object_id": "all",
        "after": json.dumps({"count": expected_count}),
    }
    rendered = json.dumps(
        {"sql": audit_sql, "params": audit_params},
        default=str,
    ).casefold()
    for forbidden in (
        "phone",
        "mobile",
        "enc",
        "mask",
        "key",
        "hash",
        "hmac",
        "ciphertext-only",
        "138****8000",
        "a" * 64,
    ):
        assert forbidden not in rendered
