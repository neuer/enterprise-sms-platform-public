from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.auth_provider import (
    ExternalRoleMapping,
    ProviderTestResult,
    UntestedProviderConfig,
)
from app.services.auth_provider_repository import SqlAuthProviderRepository

NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)


def provider_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 2,
        "code": "ad",
        "name": "AD 账号",
        "kind": "ldap",
        "enabled": False,
        "draft_config": {},
        "active_config": None,
        "draft_version": 1,
        "tested_version": None,
        "active_version": None,
        "last_tested_at": None,
        "last_test_status": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(updates)
    return row


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

    def scalar_one_or_none(self) -> object | None:
        return self.scalar


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

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository(results: list[FakeResult]) -> tuple[SqlAuthProviderRepository, FakeConnection]:
    connection = FakeConnection(results)
    repo = SqlAuthProviderRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


@pytest.mark.asyncio
async def test_save_draft_locks_provider_invalidates_test_and_audits_version_only() -> None:
    config: dict[str, object] = {
        "server": "ldaps://dc01.example.com:636",
        "base_dn": "DC=example,DC=com",
        "bind_dn": "CN=reader,DC=example,DC=com",
        "user_search_filter": "(uid={username})",
    }
    repo, connection = repository(
        [
            FakeResult([provider_row()]),
            FakeResult([provider_row(draft_config=config, draft_version=2)]),
            FakeResult(),
        ]
    )

    saved = await repo.save_draft("ad", config, actor="admin", ip="10.0.0.8")

    assert saved.draft_version == 2 and saved.tested_version is None
    assert "FOR UPDATE" in connection.calls[0][0]
    update_sql, update_params = connection.calls[1]
    assert "draft_version=draft_version+1" in update_sql
    assert "tested_version=NULL" in update_sql
    assert update_params["config"] == json.dumps(config, ensure_ascii=False)
    audit_sql, audit_params = connection.calls[2]
    assert "INSERT INTO audit_log" in audit_sql
    assert json.loads(audit_params["audit"]) == {
        "provider_code": "ad",
        "version": 2,
        "action": "save_draft",
        "result_code": "SAVED",
    }
    assert "server" not in audit_params["audit"]
    assert "bind_dn" not in audit_params["audit"]


@pytest.mark.asyncio
async def test_test_result_is_version_guarded_and_failure_clears_old_qualification() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    provider_row(
                        draft_version=3,
                        tested_version=None,
                        last_tested_at=NOW,
                        last_test_status="failed",
                    )
                ]
            ),
            FakeResult(),
        ]
    )

    saved = await repo.record_test(
        "ad",
        3,
        ProviderTestResult(False, "LDAP_BIND_FAILED"),
        actor="admin",
        ip="10.0.0.8",
    )

    update_sql, update_params = connection.calls[0]
    assert "WHERE code=:code AND draft_version=:version" in update_sql
    assert "tested_version=CASE WHEN :success" in update_sql
    assert update_params["success"] is False
    assert saved.tested_version is None and saved.last_test_status == "failed"
    audit_params = connection.calls[1][1]
    assert json.loads(audit_params["audit"]) == {
        "provider_code": "ad",
        "version": 3,
        "action": "test",
        "result_code": "LDAP_BIND_FAILED",
    }


@pytest.mark.asyncio
async def test_activate_is_atomic_and_requires_current_tested_version() -> None:
    config = {"server": "ldaps://dc01.example.com:636"}
    repo, connection = repository(
        [
            FakeResult(
                [
                    provider_row(
                        enabled=True,
                        draft_config=config,
                        active_config=config,
                        draft_version=4,
                        tested_version=4,
                        active_version=4,
                    )
                ]
            ),
            FakeResult(),
        ]
    )

    active = await repo.activate("ad", actor="admin", ip="10.0.0.8")

    sql, params = connection.calls[0]
    assert "active_config=draft_config" in sql
    assert "active_version=draft_version" in sql
    assert "tested_version=draft_version" in sql
    assert "enabled=TRUE" in sql
    assert active.enabled and active.active_version == 4
    audit = json.loads(connection.calls[1][1]["audit"])
    assert audit == {
        "provider_code": "ad",
        "version": 4,
        "action": "activate",
        "result_code": "ACTIVATED",
    }
    assert params == {"code": "ad"}


@pytest.mark.asyncio
async def test_activate_rejects_untested_draft_without_audit() -> None:
    repo, connection = repository([FakeResult()])

    with pytest.raises(UntestedProviderConfig):
        await repo.activate("ad", actor="admin", ip="10.0.0.8")

    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_disable_preserves_both_draft_and_active_configuration() -> None:
    draft = {"base_dn": "DC=new,DC=example,DC=com"}
    active = {"base_dn": "DC=example,DC=com"}
    repo, connection = repository(
        [
            FakeResult([provider_row(enabled=True, draft_config=draft, active_config=active)]),
            FakeResult([provider_row(enabled=False, draft_config=draft, active_config=active)]),
            FakeResult(),
        ]
    )

    disabled = await repo.disable("ad", actor="admin", ip="10.0.0.8")

    assert "FOR UPDATE" in connection.calls[0][0]
    assert "SET enabled=FALSE" in connection.calls[1][0]
    assert "draft_config" not in connection.calls[1][0].split("RETURNING", maxsplit=1)[0]
    assert disabled.draft_config == draft and disabled.active_config == active
    audit = json.loads(connection.calls[2][1]["audit"])
    assert audit["provider_code"] == "ad" and audit["action"] == "disable"


@pytest.mark.asyncio
async def test_role_mapping_replace_is_provider_scoped_atomic_and_safely_audited() -> None:
    mappings = (
        ExternalRoleMapping("CN=SMS-Admins,OU=Groups,DC=example,DC=com", "admin"),
        ExternalRoleMapping("CN=SMS-Operators,OU=Groups,DC=example,DC=com", "operator"),
    )
    repo, connection = repository(
        [
            FakeResult(scalar=2),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )

    saved = await repo.replace_role_mappings(
        "ad",
        mappings,
        actor="admin",
        ip="10.0.0.8",
    )

    assert saved == mappings
    assert "FOR UPDATE" in connection.calls[0][0]
    assert "DELETE FROM external_role_mapping" in connection.calls[1][0]
    assert "INSERT INTO external_role_mapping" in connection.calls[2][0]
    assert connection.calls[2][1]["provider_id"] == 2
    audit_sql, audit_params = connection.calls[-1]
    assert "INSERT INTO audit_log" in audit_sql
    assert json.loads(audit_params["audit"]) == {
        "provider_code": "ad",
        "mappings": [
            {"external_group": mappings[0].external_group, "role": "admin"},
            {"external_group": mappings[1].external_group, "role": "operator"},
        ],
    }


@pytest.mark.asyncio
async def test_role_mapping_list_orders_groups_and_returns_no_provider_secret() -> None:
    repo, connection = repository(
        [
            FakeResult(scalar=2),
            FakeResult(
                [
                    {"external_group": "CN=SMS-Admins", "role": "admin"},
                    {"external_group": "CN=SMS-Operators", "role": "operator"},
                ]
            ),
        ]
    )

    values = await repo.list_role_mappings("ad")

    assert values == (
        ExternalRoleMapping("CN=SMS-Admins", "admin"),
        ExternalRoleMapping("CN=SMS-Operators", "operator"),
    )
    sql = connection.calls[1][0]
    assert "ORDER BY external_group" in sql
    assert "draft_config" not in sql and "active_config" not in sql
