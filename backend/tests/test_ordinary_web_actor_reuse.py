"""普通路由复用请求内权威主体；真实依赖图不能绕过认证前置边界。"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler

CASES = [
    ("callbacks", "GET", "/api/v1/web/admin/callbacks", None),
    ("callbacks", "POST", "/api/v1/web/admin/callbacks/1/retry", None),
    ("blacklist", "GET", "/api/v1/web/admin/blacklist", None),
    ("blacklist", "POST", "/api/v1/web/admin/blacklist", {"phones": ["19900000000"]}),
    ("blacklist", "DELETE", "/api/v1/web/admin/blacklist/" + "a" * 64, None),
    ("sensitive_words", "GET", "/api/v1/web/admin/sensitive-words", None),
    ("sensitive_words", "POST", "/api/v1/web/admin/sensitive-words", {"words": ["synthetic"]}),
    ("sensitive_words", "DELETE", "/api/v1/web/admin/sensitive-words/1", None),
    ("approvals", "GET", "/api/v1/web/approvals", None),
    ("approvals", "GET", "/api/v1/web/approvals/1", None),
    (
        "approvals",
        "POST",
        "/api/v1/web/approvals/1/decision",
        {"action": "approve", "expected_generation": 1},
    ),
]


@pytest.mark.parametrize("module_name,method,path,payload", CASES)
@pytest.mark.parametrize(
    "token", [None, "wrong_scheme", "invalid", "revoked", "wrong_role", "valid", "valid_admin"],
)
def test_ordinary_route_verifies_once_before_business_construction(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    token: str | None,
) -> None:
    module = importlib.import_module("app.api." + module_name)
    verifies: list[str] = []
    constructions: list[str] = []
    auditor_constructions: list[str] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    role = "approver" if module_name == "approvals" and token != "valid_admin" else "admin"
    claims = JwtClaims(8, 18, "local", "synthetic", "synthetic", "synthetic", role)

    class Facade:
        async def verify(self, value: str) -> JwtClaims:
            verifies.append(value)
            if value in {"invalid", "revoked"}:
                raise ApiError(401, "UNAUTHORIZED", "synthetic invalid session", None)
            if value == "wrong_role":
                return JwtClaims(8, 18, "local", "synthetic", "synthetic", "synthetic", "viewer")
            return claims

    class Business:
        async def list_page(self, **kwargs: Any) -> Any:
            calls.append(("list_page", kwargs))
            if module_name == "callbacks":
                return {"total": 0, "dead_total": 0, "items": []}
            if module_name == "approvals":
                return {"total": 0, "items": []}
            return SimpleNamespace(total=0, items=[])

        async def counts(self, **kwargs: Any) -> dict[str, int]:
            calls.append(("counts", kwargs))
            return dict.fromkeys(
                ["pending", "approved", "rejected", "expired", "pending_urgent"], 0,
            )

        async def get_detail(self, _: int) -> dict[str, Any]:
            calls.append(("get_detail", {}))
            now = datetime.now(UTC)
            return {
                "id": 1, "batch_no": "synthetic", "category": "notice", "applicant": "other",
                "applicant_account_id": 9, "dept": "synthetic", "total": 1, "segments": 1,
                "estimated_segments": 1, "scheduled_at": None, "trigger_threshold": 1,
                "trigger_threshold_source": "snapshot", "batch_status": "pending_approval",
                "deferred_reason": None, "status": "pending", "approver": None, "reason": None,
                "expires_at": now, "decided_at": None, "created_at": now, "generation": 1,
                "content": "synthetic",
            }

        async def manual_retry(self, _: int, **kwargs: Any) -> None:
            calls.append(("manual_retry", kwargs))

        async def add(self, _: Any, **kwargs: Any) -> Any:
            calls.append(("add", kwargs))
            return SimpleNamespace(added=0, updated=0, entries=[], created=[], skipped=0)

        async def delete(self, _: Any, **kwargs: Any) -> bool:
            calls.append(("delete", kwargs))
            return True

        async def decide(self, _: Any, **kwargs: Any) -> Any:
            calls.append(("decide", kwargs))
            return SimpleNamespace(status="approved", batch_status="queued", deferred_reason=None)

    class Redis:
        @classmethod
        def from_url(cls, *_: Any, **__: Any) -> Redis:
            return cls()

        async def aclose(self) -> None:
            pass

    class Auditor:
        async def record(self, **_: Any) -> None:
            pass

    def auditor(*_: Any) -> Auditor:
        auditor_constructions.append("auditor")
        return Auditor()

    def repository(*_: Any, **__: Any) -> Business:
        constructions.append("business")
        return Business()

    settings = SimpleNamespace(redis_control_url="redis://synthetic")
    if module_name == "callbacks":
        monkeypatch.setattr(module, "SqlCallbackRepository", repository)
    elif module_name == "blacklist":
        monkeypatch.setattr(module, "SqlBlacklistRepository", repository)
        monkeypatch.setattr(module, "BlacklistService", lambda repo, *_: repo)
    elif module_name == "sensitive_words":
        monkeypatch.setattr(module, "SqlSensitiveWordRepository", repository)
        monkeypatch.setattr(module, "SensitiveWordManager", lambda repo, *_: repo)
    else:
        from app.services import sensitive_read_audit

        monkeypatch.setattr(module, "SqlApprovalRepository", repository)
        monkeypatch.setattr(module, "ApprovalService", lambda repo, *_: repo)
        monkeypatch.setattr(module, "CeleryQueuePublisher", lambda: object())
        monkeypatch.setattr(module, "SqlAlertService", lambda *_: object())
        monkeypatch.setattr(sensitive_read_audit, "get_settings", lambda: settings)
        monkeypatch.setattr(sensitive_read_audit, "SensitiveReadAuditor", auditor)
    if hasattr(module, "get_settings"):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(module, "Redis", Redis)
        monkeypatch.setattr(
            module, "CryptoService", SimpleNamespace(from_settings=lambda _: object()),
        )

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_auth_facade] = Facade
    app.include_router(module.router)
    headers = {} if token is None else {
        "Authorization": "Basic invalid" if token == "wrong_scheme" else f"Bearer {token}",
    }
    with TestClient(app) as client:
        response = client.request(method, path, json=payload, headers=headers)
        if token in {"valid", "valid_admin"}:
            assert response.status_code in {200, 204}, response.text
            assert verifies == [token]
            assert constructions == ["business"]
            assert auditor_constructions == (["auditor"] if path.endswith("approvals/1") else [])
            assert calls
            assert len({name for name, _ in calls}) == len(calls)
            for _, values in calls:
                if "principal" in values:
                    assert values["principal"] == claims.principal
                if "actor" in values:
                    assert values["actor"] == claims.username
            # FastAPI 的依赖缓存不得跨请求保留权威主体。
            second = client.request(method, path, json=payload, headers=headers)
            assert second.status_code == response.status_code
            assert verifies == [token, token]
            assert constructions == ["business", "business"]
        else:
            assert response.status_code == (403 if token == "wrong_role" else 401), response.text
            assert verifies == ([] if token in {None, "wrong_scheme"} else [token])
            assert constructions == [] and calls == [] and auditor_constructions == []
