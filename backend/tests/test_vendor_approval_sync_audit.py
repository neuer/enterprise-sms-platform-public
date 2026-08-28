from __future__ import annotations

from typing import Any

import pytest

import app.services.sign_repository as sign_repository_module
import app.services.template_repository as template_repository_module
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services.sign_management import SignStateConflict
from app.services.sign_repository import SqlSignRepository
from app.services.template_repository import SqlTemplateRepository


class FakeResult:
    def __init__(self, scalar: object = None) -> None:
        self.scalar = scalar

    def scalar_one_or_none(self) -> object:
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

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


@pytest.mark.parametrize(
    ("repository", "object_type", "repository_module"),
    (
        (SqlTemplateRepository(), "template", template_repository_module),
        (SqlSignRepository(), "sign", sign_repository_module),
    ),
)
@pytest.mark.asyncio
async def test_automatic_approval_sync_audits_only_effective_transition(
    repository: SqlTemplateRepository | SqlSignRepository,
    object_type: str,
    repository_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([FakeResult(7), FakeResult(), FakeResult(None)])
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    bindings: list[tuple[str, str]] = []

    async def bind_system(
        _connection: object, *, actor_name: str, action: str
    ) -> None:
        bindings.append((actor_name, action))

    monkeypatch.setattr(repository_module, "bind_connection_system_audit", bind_system)

    applied = await repository.apply_states(
        [(7, "approved", None), (8, "rejected", "vendor detail")]
    )

    assert applied == 1
    assert bindings == [("vendor-state-sync", f"{object_type}_sync")]
    assert "RETURNING id" in connection.calls[0][0]
    audit_sql, audit_params = connection.calls[1]
    assert "'vendor-state-sync','system','system'" in audit_sql
    assert f"'{object_type}'" in audit_sql
    assert audit_params == {"id": 7, "state": "approved"}
    assert "vendor detail" not in str(audit_params)


@pytest.mark.parametrize(
    ("repository", "object_type", "repository_module"),
    (
        (SqlTemplateRepository(), "template", template_repository_module),
        (SqlSignRepository(), "sign", sign_repository_module),
    ),
)
@pytest.mark.asyncio
async def test_vendor_binding_result_is_system_audited_without_vendor_id_in_payload(
    repository: SqlTemplateRepository | SqlSignRepository,
    object_type: str,
    repository_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([FakeResult(7), FakeResult()])
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    bindings: list[tuple[str, str]] = []

    async def bind_system(
        _connection: object, *, actor_name: str, action: str
    ) -> None:
        bindings.append((actor_name, action))

    monkeypatch.setattr(repository_module, "bind_connection_system_audit", bind_system)

    applied = await repository.apply_binding(7, "private-vendor-reference")

    assert applied is True
    assert bindings == [("vendor-state-sync", f"{object_type}_sync")]
    audit_sql, audit_params = connection.calls[1]
    assert "'vendor-state-sync','system','system'" in audit_sql
    assert audit_params == {"id": 7}
    assert "private-vendor-reference" not in audit_sql
    assert "private-vendor-reference" not in str(audit_params)


@pytest.mark.asyncio
async def test_existing_sign_adoption_is_cas_bound_and_system_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [FakeResult(), FakeResult(None), FakeResult(7), FakeResult()]
    )
    repository = SqlSignRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    bindings: list[tuple[str, str]] = []

    async def bind_system(
        _connection: object, *, actor_name: str, action: str
    ) -> None:
        bindings.append((actor_name, action))

    monkeypatch.setattr(
        sign_repository_module,
        "bind_connection_system_audit",
        bind_system,
    )

    applied = await repository.adopt_existing(
        7,
        "112074",
        "approved",
        None,
    )

    assert applied is True
    assert bindings == [("vendor-state-sync", "sign_adopt")]
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "vendor_sign_id=:vendor_sign_id" in connection.calls[2][0]
    assert "vendor_state='pending'" in connection.calls[2][0]
    audit_sql, audit_params = connection.calls[3]
    assert "'sign_adopt'" in audit_sql
    assert audit_params == {
        "id": 7,
        "vendor_state": "approved",
        "vendor_sign_id": "112074",
    }
    assert "vendor_sign_id" in audit_sql


@pytest.mark.asyncio
async def test_existing_vendor_sign_id_cannot_be_adopted_twice() -> None:
    connection = FakeConnection([FakeResult(), FakeResult(8)])
    repository = SqlSignRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    with pytest.raises(SignStateConflict, match="其他本地签名"):
        await repository.adopt_existing(7, "112074", "approved", None)

    assert len(connection.calls) == 2


@pytest.mark.parametrize(
    ("repository", "action"),
    (
        (SqlTemplateRepository(), "template_sync"),
        (SqlSignRepository(), "sign_sync"),
    ),
)
@pytest.mark.asyncio
async def test_manual_approval_sync_uses_stable_human_attribution(
    repository: SqlTemplateRepository | SqlSignRepository,
    action: str,
) -> None:
    connection = FakeConnection([FakeResult(9), FakeResult()])
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    principal = SecurityPrincipal(3, 4, "admin01", "平台部", "admin")

    with audit_principal_scope(principal), correlation_scope():
        applied = await repository.apply_states([(9, "rejected", "材料不足")])

    assert applied == 1
    audit_sql, audit_params = connection.calls[1]
    assert "INSERT INTO audit_log" in audit_sql
    assert audit_params["subject_kind"] == "human"
    assert audit_params["account_id"] == 3
    assert audit_params["identity_id"] == 4
    assert audit_params["action"] == action
    assert audit_params["after"] == '{"vendor_state": "rejected"}'
    assert "材料不足" not in str(audit_params)
