from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports as module
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.roles import Role
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.export import (
    ExportForbidden,
    ExportRequestFilters,
    ExportTaskInfo,
)

PUBLIC_ID = UUID("c0a80101-0000-4000-8000-000000000134")


class FakeFacade:
    def __init__(self, role: Role) -> None:
        self.role: Role = role

    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims(
            11,
            101,
            "local",
            "tester",
            "测试用户",
            "平台部",
            self.role,
            1,
            "session-jti",
        )


class FakeService:
    def __init__(self, *, decrypted: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self.now = datetime.now(UTC)
        self.decrypted = decrypted

    async def create(
        self,
        filters: ExportRequestFilters,
        *,
        decrypted: bool,
        principal: SecurityPrincipal,
    ) -> ExportTaskInfo:
        self.calls.append(
            (
                "create",
                {
                    "filters": filters,
                    "decrypted": decrypted,
                    "actor": principal.login_name,
                    "actor_account_id": principal.account_id,
                    "role": principal.role,
                    "dept": principal.dept,
                },
            )
        )
        if decrypted and principal.role not in {"approver", "admin"}:
            raise ExportForbidden("当前角色无明文导出权限")
        return ExportTaskInfo(
            9,
            PUBLIC_ID,
            "pending",
            decrypted,
            None,
            None,
            None,
            self.now,
        )

    async def get(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
    ) -> ExportTaskInfo:
        self.calls.append(
            (
                "get",
                {
                    "public_id": public_id,
                    "actor_account_id": principal.account_id,
                    "role": principal.role,
                    "dept": principal.dept,
                },
            )
        )
        return ExportTaskInfo(
            9,
            public_id,
            "done",
            self.decrypted,
            2,
            "/safe/export-9.smsx",
            self.now + timedelta(days=7),
            self.now,
        )

    async def get_downloadable(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> ExportTaskInfo:
        self.calls.append(
            (
                "download",
                {
                    "public_id": public_id,
                    "actor": principal.login_name,
                    "actor_account_id": principal.account_id,
                    "role": principal.role,
                    "dept": principal.dept,
                    "ip": ip,
                },
            )
        )
        return await self.get(
            public_id,
            principal=principal,
        )


class FakeStepUp:
    def __init__(self) -> None:
        self.issues: list[tuple[UUID, str, int, str]] = []
        self.consumes: list[tuple[str, UUID, int, str]] = []

    async def issue(
        self,
        *,
        claims: JwtClaims,
        password: str,
        ip: str,
        public_id: UUID,
    ) -> str:
        self.issues.append((public_id, password, claims.account_id, ip))
        return "one-use-export-token"

    async def consume(
        self,
        token: str,
        *,
        claims: JwtClaims,
        ip: str,
        public_id: UUID,
    ) -> None:
        self.consumes.append((token, public_id, claims.account_id, ip))


class FakeCodec:
    def validate(self, raw_path: str | Path, *, expected_task_id: int) -> Path:
        assert str(raw_path) == "/safe/export-9.smsx"
        assert expected_task_id == 9
        return Path(str(raw_path))

    def iter_decrypted(self, _path: str | Path) -> Iterator[bytes]:
        yield b"phone,status\r\n"
        yield b"138****8000,delivered\r\n"


def make_client(
    role: Role = "viewer",
    *,
    decrypted: bool = False,
) -> tuple[TestClient, FakeService, FakeStepUp]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    service = FakeService(decrypted=decrypted)
    step_up = FakeStepUp()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[module.get_export_service] = lambda: service
    app.dependency_overrides[module.get_export_step_up_service] = lambda: step_up
    app.dependency_overrides[module.get_export_codec] = FakeCodec
    app.include_router(module.router)
    return TestClient(app, client=("127.0.0.1", 50000)), service, step_up


def test_create_masked_export_returns_202_and_normalized_request() -> None:
    client, service, _ = make_client()
    response = client.post(
        "/api/v1/web/reports/export",
        headers={"Authorization": "Bearer jwt"},
        json={"filters": {"phone": "13800138000", "category": "notice"}},
    )
    assert response.status_code == 202
    assert response.json()["id"] == str(PUBLIC_ID)
    assert response.json()["status"] == "pending"
    values = cast(dict[str, Any], service.calls[0][1])
    assert values["filters"].phone == "13800138000"
    assert values["actor_account_id"] == 11
    assert vars(module.create_export)["__audited_action__"] == "export_create"


def test_viewer_cannot_create_decrypted_export() -> None:
    client, _, _ = make_client()
    response = client.post(
        "/api/v1/web/reports/export",
        headers={"Authorization": "Bearer jwt"},
        json={"filters": {}, "decrypted": True},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_status_returns_download_url_only_for_ready_unexpired_task() -> None:
    client, _, _ = make_client(role="approver")
    response = client.get(
        f"/api/v1/web/reports/export/{PUBLIC_ID}",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    assert response.json()["download_url"] == (
        f"/api/v1/web/reports/export/{PUBLIC_ID}/download"
    )
    assert response.json()["expires_at"] is not None


def test_download_is_streamed_no_store_and_never_persists_plaintext() -> None:
    client, service, step_up = make_client()
    response = client.get(
        f"/api/v1/web/reports/export/{PUBLIC_ID}/download",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.endswith(b"138****8000,delivered\r\n")
    assert any(call[0] == "download" for call in service.calls)
    assert step_up.consumes == []


def test_decrypted_download_requires_task_bound_single_use_step_up() -> None:
    client, service, step_up = make_client(role="approver", decrypted=True)
    headers = {"Authorization": "Bearer jwt"}

    denied = client.get(
        f"/api/v1/web/reports/export/{PUBLIC_ID}/download",
        headers=headers,
    )
    assert denied.status_code == 401
    assert denied.json()["code"] == "STEP_UP_REQUIRED"
    assert all(call[0] != "download" for call in service.calls)

    issued = client.post(
        f"/api/v1/web/reports/export/{PUBLIC_ID}/step-up",
        headers=headers,
        json={"password": "current-password"},
    )
    assert issued.status_code == 200
    assert issued.json() == {"token": "one-use-export-token", "expires_in": 300}
    assert step_up.issues == [(PUBLIC_ID, "current-password", 11, "127.0.0.1")]

    downloaded = client.get(
        f"/api/v1/web/reports/export/{PUBLIC_ID}/download",
        headers={**headers, "X-Export-Step-Up": issued.json()["token"]},
    )
    assert downloaded.status_code == 200
    assert step_up.consumes == [
        ("one-use-export-token", PUBLIC_ID, 11, "127.0.0.1")
    ]
    assert any(call[0] == "download" for call in service.calls)


def test_integer_export_identifier_is_not_an_external_compatibility_route() -> None:
    client, _, _ = make_client()
    response = client.get(
        "/api/v1/web/reports/export/9",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 422
