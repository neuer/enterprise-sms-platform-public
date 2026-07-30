from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.crypto import CryptoService
from app.services.export import (
    ExportFilterSet,
    ExportForbidden,
    ExportRequestFilters,
    ExportService,
    ExportTaskInfo,
    ExportTooLarge,
)

PUBLIC_ID = UUID("c0a80101-0000-4000-8000-000000000134")


def principal(
    account_id: int,
    login_name: str,
    role: str,
    dept: str = "平台部",
) -> SecurityPrincipal:
    return SecurityPrincipal(account_id, account_id + 100, login_name, dept, role)  # type: ignore[arg-type]


def crypto() -> CryptoService:
    key1 = base64.b64encode(b"1" * 32).decode()
    key2 = base64.b64encode(b"2" * 32).decode()
    return CryptoService.from_secret_values(
        '{"active_version":2,"keys":{"1":"' + key1 + '","2":"' + key2 + '"}}',
        '{"active_version":2,"keys":{"1":"' + key1 + '","2":"' + key2 + '"}}',
    )


class FakeRepository:
    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.calls: list[tuple[str, object]] = []

    async def count_rows(self, filters: ExportFilterSet) -> int:
        self.calls.append(("count", filters))
        return self.count

    async def create(
        self,
        *,
        principal: SecurityPrincipal,
        filters: ExportFilterSet,
        decrypted: bool,
    ) -> ExportTaskInfo:
        self.calls.append(
            (
                "create",
                {
                    "creator": principal.login_name,
                    "creator_account_id": principal.account_id,
                    "creator_role": principal.role,
                    "filters": filters,
                    "decrypted": decrypted,
                },
            )
        )
        return ExportTaskInfo(
            9,
            PUBLIC_ID,
            "pending",
            decrypted,
            None,
            None,
            None,
            datetime.now(UTC),
        )

    async def get_accessible(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        retention_days: int,
    ) -> ExportTaskInfo | None:
        self.calls.append(
            (
                "get",
                {
                    "public_id": public_id,
                    "actor_account_id": principal.account_id,
                    "actor_role": principal.role,
                    "actor_dept": principal.dept,
                },
            )
        )
        return ExportTaskInfo(
            9,
            public_id,
            "done",
            False,
            1,
            "/safe/export.smsx",
            datetime.now(UTC) + timedelta(days=retention_days),
            datetime.now(UTC),
        )

    async def get_downloadable_and_audit(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        ip: str,
        retention_days: int,
    ) -> ExportTaskInfo | None:
        self.calls.append(
            (
                "download",
                {
                    "public_id": public_id,
                    "actor": principal.login_name,
                    "actor_account_id": principal.account_id,
                    "actor_role": principal.role,
                    "actor_dept": principal.dept,
                    "ip": ip,
                },
            )
        )
        return ExportTaskInfo(
            9,
            public_id,
            "done",
            False,
            1,
            "/safe/export.smsx",
            datetime.now(UTC) + timedelta(days=retention_days),
            datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_masked_export_normalizes_phone_to_all_hmac_versions_and_department() -> None:
    repository = FakeRepository()
    service = ExportService(repository, crypto(), retention_days=7)

    task = await service.create(
        ExportRequestFilters(phone="13800138000", category="notice"),
        decrypted=False,
        principal=principal(11, "viewer-a", "viewer"),
    )

    assert task.id == 9
    normalized = repository.calls[0][1]
    assert isinstance(normalized, ExportFilterSet)
    assert normalized.scope_dept == "平台部"
    assert len(normalized.phone_hmacs) == 2
    assert "13800138000" not in repr(repository.calls)
    assert normalized.safe_json()["phone_hmacs"] == list(normalized.phone_hmacs)


@pytest.mark.asyncio
async def test_decrypted_export_and_cross_department_require_elevated_role() -> None:
    repository = FakeRepository()
    service = ExportService(repository, crypto(), retention_days=7)
    with pytest.raises(ExportForbidden):
        await service.create(
            ExportRequestFilters(),
            decrypted=True,
            principal=principal(11, "viewer-a", "viewer"),
        )
    with pytest.raises(ExportForbidden):
        await service.create(
            ExportRequestFilters(dept="财务部"),
            decrypted=False,
            principal=principal(12, "operator-a", "operator"),
        )
    assert repository.calls == []


@pytest.mark.asyncio
async def test_admin_can_scope_export_and_row_limit_is_enforced_before_creation() -> None:
    repository = FakeRepository(count=100_001)
    service = ExportService(repository, crypto(), retention_days=7)
    with pytest.raises(ExportTooLarge):
        await service.create(
            ExportRequestFilters(dept="财务部"),
            decrypted=True,
            principal=principal(1, "admin-a", "admin"),
        )
    filters = repository.calls[0][1]
    assert isinstance(filters, ExportFilterSet) and filters.scope_dept == "财务部"
    assert all(call[0] != "create" for call in repository.calls)


@pytest.mark.asyncio
async def test_export_time_range_must_be_aware_and_ordered() -> None:
    service = ExportService(FakeRepository(), crypto(), retention_days=7)
    with pytest.raises(ValueError, match="timezone"):
        await service.create(
            ExportRequestFilters(start=datetime(2026, 7, 1)),
            decrypted=False,
            principal=principal(11, "viewer-a", "viewer"),
        )
    with pytest.raises(ValueError, match="later"):
        await service.create(
            ExportRequestFilters(
                start=datetime(2026, 7, 2, tzinfo=UTC),
                end=datetime(2026, 7, 1, tzinfo=UTC),
            ),
            decrypted=False,
            principal=principal(11, "viewer-a", "viewer"),
        )


@pytest.mark.asyncio
async def test_status_access_passes_stable_subject_and_explicit_scope() -> None:
    repository = FakeRepository()
    service = ExportService(repository, crypto(), retention_days=7)
    task = await service.get(
        PUBLIC_ID,
        principal=principal(21, "approver-a", "approver"),
    )
    assert task.status == "done"
    assert repository.calls[-1] == (
        "get",
        {
            "public_id": PUBLIC_ID,
            "actor_account_id": 21,
            "actor_role": "approver",
            "actor_dept": "平台部",
        },
    )


@pytest.mark.asyncio
async def test_download_access_is_rechecked_and_transactionally_audited() -> None:
    repository = FakeRepository()
    service = ExportService(repository, crypto(), retention_days=7)

    task = await service.get_downloadable(
        PUBLIC_ID,
        principal=principal(21, "approver-a", "approver"),
        ip="10.0.0.8",
    )

    assert task.public_id == PUBLIC_ID
    assert repository.calls[-1] == (
        "download",
        {
            "public_id": PUBLIC_ID,
            "actor": "approver-a",
            "actor_account_id": 21,
            "actor_role": "approver",
            "actor_dept": "平台部",
            "ip": "10.0.0.8",
        },
    )


@pytest.mark.asyncio
async def test_unmatched_dataset_is_admin_only_and_roundtrips_safe_filters() -> None:
    repository = FakeRepository()
    service = ExportService(repository, crypto(), retention_days=7)
    with pytest.raises(ExportForbidden):
        await service.create(
            ExportRequestFilters(dataset="unmatched", phone="13800138000"),
            decrypted=False,
            principal=principal(21, "approver-a", "approver"),
        )

    task = await service.create(
        ExportRequestFilters(dataset="unmatched", phone="13800138000"),
        decrypted=True,
        principal=principal(1, "admin-a", "admin"),
    )

    assert task.id == 9
    normalized = repository.calls[-2][1]
    assert isinstance(normalized, ExportFilterSet)
    assert normalized.dataset == "unmatched"
    assert ExportFilterSet.from_safe_json(normalized.safe_json()) == normalized
    legacy = normalized.safe_json()
    legacy.pop("dataset")
    assert ExportFilterSet.from_safe_json(legacy).dataset == "message"
