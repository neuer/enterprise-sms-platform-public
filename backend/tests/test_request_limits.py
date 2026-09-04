from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.request_limits import (
    API_JSON_BODY_LIMIT,
    IMPORT_BODY_LIMIT,
    IMPORT_PATH,
    RequestBodyLimitMiddleware,
    body_limit_for,
)


async def echo(request: object) -> JSONResponse:
    body = await request.body()  # type: ignore[attr-defined]
    return JSONResponse({"n": len(body)})


def _client() -> TestClient:
    inner = Starlette(
        routes=[
            Route("/api/v1/messages/send", echo, methods=["POST"]),
            Route(IMPORT_PATH, echo, methods=["POST"]),
            Route("/readyz", echo, methods=["GET", "HEAD", "POST"]),
        ]
    )
    return TestClient(RequestBodyLimitMiddleware(inner))


def test_body_limit_for_matches_nginx_routes() -> None:
    assert body_limit_for("/api/v1/messages/send", "POST") == API_JSON_BODY_LIMIT
    assert body_limit_for(IMPORT_PATH, "POST") == IMPORT_BODY_LIMIT
    assert body_limit_for(IMPORT_PATH, "GET") == 0
    assert body_limit_for("/readyz", "GET") == 0
    assert body_limit_for("/readyz", "POST") == 0
    assert body_limit_for("/readyz", "HEAD") == 0


def test_health_and_get_routes_reject_body_before_handler() -> None:
    client = _client()
    for method, path in (("GET", "/readyz"), ("POST", "/readyz"), ("HEAD", "/readyz")):
        response = client.request(
            method,
            path,
            content=b"x",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        if method == "HEAD":
            assert response.content == b""
        else:
            assert response.json()["code"] == "INVALID_PARAM"


def test_oversized_json_is_rejected_before_route() -> None:
    response = _client().post(
        "/api/v1/messages/send",
        content=b"x" * (API_JSON_BODY_LIMIT + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {
        "code": "PAYLOAD_TOO_LARGE",
        "message": "请求体超过上限",
        "detail": None,
    }
    assert "x" not in response.text


def test_import_route_allows_larger_body_than_json_api() -> None:
    client = _client()
    allowed = client.post(IMPORT_PATH, content=b"y" * (API_JSON_BODY_LIMIT + 8))
    assert allowed.status_code == 200
    assert allowed.json()["n"] == API_JSON_BODY_LIMIT + 8
    denied = client.post(IMPORT_PATH, content=b"y" * (IMPORT_BODY_LIMIT + 1))
    assert denied.status_code == 413
    assert denied.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_no_body_rejects_chunked_encoding_before_handler() -> None:
    response = _client().request(
        "GET",
        "/readyz",
        content=b"x",
        headers={"transfer-encoding": "chunked"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"


def test_declared_content_length_over_limit_is_rejected() -> None:
    response = _client().post(
        "/api/v1/messages/send",
        content=b"ok",
        headers={"content-length": str(API_JSON_BODY_LIMIT + 10)},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
