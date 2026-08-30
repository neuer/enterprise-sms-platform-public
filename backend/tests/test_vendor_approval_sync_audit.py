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
from app.services.template_management import TemplateStateConflict
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

    states = (
        [
            (7, "21", 101, "approved", None),
            (8, "22", 102, "rejected", "vendor detail"),
        ]
        if object_type == "template"
        else [
            (7, "31", 201, "approved", None),
            (8, "32", 202, "rejected", "vendor detail"),
        ]
    )
    applied = await repository.apply_states(states)  # type: ignore[arg-type]

    assert applied == 1
    assert bindings == [("vendor-state-sync", f"{object_type}_sync")]
    update_sql, update_params = connection.calls[0]
    assert "RETURNING id" in update_sql
    assert "xmin::text::bigint=:expected_row_version" in update_sql
    expected_id_key = (
        "expected_vendor_template_id"
        if object_type == "template"
        else "expected_vendor_sign_id"
    )
    assert update_params[expected_id_key] in {"21", "31"}
    audit_sql, audit_params = connection.calls[1]
    assert "'vendor-state-sync','system','system'" in audit_sql
    assert f"'{object_type}'" in audit_sql
    assert audit_params == {"id": 7, "state": "approved"}
    assert "vendor detail" not in str(audit_params)


@pytest.mark.asyncio
async def test_template_sync_sql_allows_rejected_and_skips_exact_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([FakeResult(None)])
    repository = SqlTemplateRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    bindings: list[tuple[str, str]] = []

    async def bind_system(
        _connection: object, *, actor_name: str, action: str
    ) -> None:
        bindings.append((actor_name, action))

    monkeypatch.setattr(
        template_repository_module,
        "bind_connection_system_audit",
        bind_system,
    )

    applied = await repository.apply_states([(7, "21", 77, "rejected", "材料不足")])

    assert applied == 0
    update_sql = connection.calls[0][0]
    assert "('pending','approved','rejected')" in update_sql
    assert "vendor_template_id=:expected_vendor_template_id" in update_sql
    assert "xmin::text::bigint=:expected_row_version" in update_sql
    assert "vendor_state IS DISTINCT FROM :state" in update_sql
    assert "vendor_reject_reason IS DISTINCT FROM :reason" in update_sql
    assert connection.calls[0][1]["expected_vendor_template_id"] == "21"
    assert connection.calls[0][1]["expected_row_version"] == 77
    assert bindings == []


@pytest.mark.asyncio
async def test_template_sync_drops_old_vendor_result_after_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧厂商编号返回结果时，新编号已绑定则 CAS 不更新也不审计。"""

    connection = FakeConnection([FakeResult(None)])
    repository = SqlTemplateRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    bindings: list[tuple[str, str]] = []

    async def bind_system(
        _connection: object, *, actor_name: str, action: str
    ) -> None:
        bindings.append((actor_name, action))

    monkeypatch.setattr(
        template_repository_module,
        "bind_connection_system_audit",
        bind_system,
    )

    applied = await repository.apply_states([(7, "21", 77, "approved", None)])

    assert applied == 0
    update_sql, params = connection.calls[0]
    assert "vendor_template_id=:expected_vendor_template_id" in update_sql
    assert "xmin::text::bigint=:expected_row_version" in update_sql
    assert params["expected_vendor_template_id"] == "21"
    assert params["expected_row_version"] == 77
    assert bindings == []


@pytest.mark.asyncio
async def test_rejected_sign_update_is_blocked_when_referenced() -> None:
    connection = FakeConnection([FakeResult(None)])
    repository = SqlSignRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    updated = await repository.update(7, name="新签名", actor="admin01")

    assert updated is None
    update_sql = connection.calls[0][0]
    assert "FROM app a" in update_sql
    assert "a.default_sign IN (s.name,'【' || s.name || '】')" in update_sql
    assert "FROM sms_batch b" in update_sql


@pytest.mark.asyncio
async def test_rejected_sign_delete_matches_plain_and_formatted_app_default() -> None:
    connection = FakeConnection([FakeResult("青鸾"), FakeResult(None)])
    repository = SqlSignRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    assert await repository.delete(7, actor="admin01") is False

    delete_sql = connection.calls[1][0]
    assert "a.default_sign IN (s.name,'【' || s.name || '】')" in delete_sql


@pytest.mark.asyncio
async def test_template_binding_rejects_duplicate_vendor_id_before_update() -> None:
    connection = FakeConnection([FakeResult(), FakeResult(8)])
    repository = SqlTemplateRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    with pytest.raises(TemplateStateConflict, match="已关联"):
        await repository.apply_binding(7, "21")

    assert len(connection.calls) == 2
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "SELECT id FROM sms_template" in connection.calls[1][0]


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
    connection = FakeConnection(
        [FakeResult(), FakeResult(None), FakeResult(7), FakeResult()]
    )
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
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    audit_sql, audit_params = connection.calls[3]
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
        states = (
            [(9, "21", 90, "rejected", "材料不足")]
            if action == "template_sync"
            else [(9, "31", 90, "rejected", "材料不足")]
        )
        applied = await repository.apply_states(states)  # type: ignore[arg-type]

    assert applied == 1
    audit_sql, audit_params = connection.calls[1]
    assert "INSERT INTO audit_log" in audit_sql
    assert audit_params["subject_kind"] == "human"
    assert audit_params["account_id"] == 3
    assert audit_params["identity_id"] == 4
    assert audit_params["action"] == action
    assert audit_params["after"] == '{"vendor_state": "rejected"}'
    assert "材料不足" not in str(audit_params)
