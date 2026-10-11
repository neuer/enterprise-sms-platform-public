"""运维客户端的公开合同与失败边界；不以脚本假成功替代业务验收。"""
from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.admin_step_up import admin_intent
from app.services.app_contract import AppCreateRequest, AppUpdateRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import admin_write_client  # noqa: E402
from admin_write_client import AdminWriteClient, complete_app_config  # noqa: E402

REVISION = "2026-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class Response:
    status: int
    data: object


class Transport:
    def __init__(self, responses: list[Response]):
        self.responses = responses
        self.calls: list[tuple[str, str, object, object]] = []

    def request(self, method, path, *, payload=None, headers=None):
        self.calls.append((method, path, deepcopy(payload), deepcopy(headers)))
        return self.responses.pop(0)


def client(transport: Transport) -> AdminWriteClient[Response]:
    return AdminWriteClient(transport, "access.jwt", "synthetic-password",
                            failure=RuntimeError, label="probe")


@pytest.mark.parametrize("create", [True, False])
def test_client_defaults_match_entire_production_request_contract(create: bool) -> None:
    values = {"name": "probe", "dept": "test"} if create else {
        "dept": "test", "allowed_categories": ["notice"], "daily_quota": 5,
        "rate_limit_per_min": 60, "blacklist_check": True,
    }
    model = AppCreateRequest if create else AppUpdateRequest
    assert complete_app_config(values, create=create) == model.model_validate(values).model_dump()
    complete_app_config(values, create=create)["allowed_ips"].append("10.0.0.0/8")
    assert complete_app_config(values, create=create)["allowed_ips"] == []
    with pytest.raises(ValueError):
        complete_app_config({**values, "api_key": "must-not-enter-intent"}, create=create)


def test_create_binds_complete_actual_parameters_and_keeps_password_out_of_business_call() -> None:
    transport = Transport([
        Response(200, {"token": "grant"}), Response(400, {"code": "INVALID_PARAM"}),
    ])
    result = client(transport).create_app({"name": "probe", "dept": "test",
                                        "callback_url": "http://127.0.0.1:9028/"})
    assert result.status == 400
    grant = transport.calls[0][2]
    assert grant["operation"] == "app_create" and grant["target_id"] == "new"
    assert grant["parameters"]["config"] == transport.calls[1][2]
    admin_intent(grant["operation"], grant["target_id"], grant["parameters"])
    assert "password" not in transport.calls[1][2]
    assert transport.calls[1][3]["X-Admin-Step-Up"] == "grant"


@pytest.mark.parametrize("status", [401, 403, 409, 422, 429, 503])
def test_grant_failure_never_calls_write_or_prints_secret(status: int) -> None:
    transport = Transport([Response(status, {"message": "secret-must-not-appear"})])
    with pytest.raises(RuntimeError) as error:
        client(transport).create_app({"name": "probe", "dept": "test"})
    assert len(transport.calls) == 1
    assert "secret" not in str(error.value) and "synthetic-password" not in str(error.value)


@pytest.mark.parametrize("revision", [None, "", "2026-01-01", "bad", 5])
def test_missing_or_invalid_revision_cannot_request_authorization(revision: object) -> None:
    transport = Transport([Response(200, {"updated_at": revision})])
    with pytest.raises(RuntimeError, match="revision"):
        client(transport).disable_app(9)
    assert [call[:2] for call in transport.calls] == [("GET", "/api/v1/web/admin/apps/9")]


def test_disable_uses_current_revision_and_does_not_retry_conflicting_mutation() -> None:
    transport = Transport([Response(200, {"updated_at": REVISION}),
                           Response(200, {"token": "grant"}), Response(409, None)])
    assert client(transport).disable_app(9).status == 409
    assert len(transport.calls) == 3
    grant = transport.calls[1][2]
    assert grant["operation"] == "app_disable" and grant["target_id"] == "9"
    assert grant["parameters"] == {"expected_updated_at": REVISION}
    assert transport.calls[-1][0] == "DELETE"
    assert transport.calls[-1][3]["X-App-Revision"] == REVISION


def test_config_binds_fresh_version_and_actual_nonsensitive_value() -> None:
    transport = Transport([Response(200, [{"key": "vendor_qps", "value": "5",
                            "sensitive": False, "updated_at": REVISION}]),
                           Response(200, {"token": "grant"}), Response(200, [])])
    assert client(transport).update_configs({"vendor_qps": "6"}).status == 200
    grant = transport.calls[1][2]
    admin_intent(grant["operation"], grant["target_id"], grant["parameters"])
    assert grant["parameters"] == transport.calls[-1][2]
    assert grant["parameters"]["items"] == [{"key": "vendor_qps", "value": "6",
                                               "expected_updated_at": REVISION}]


@pytest.mark.parametrize("sensitive", [True, None, "false"])
def test_diagnostic_client_refuses_ambiguous_or_secret_config(sensitive: object) -> None:
    transport = Transport([Response(200, [{"key": "secret", "value": None,
                            "sensitive": sensitive, "updated_at": REVISION}])])
    with pytest.raises(RuntimeError, match="non-secret"):
        client(transport).update_configs({"secret": "do-not-copy"})
    assert len(transport.calls) == 1


def test_only_explicit_admission_throttle_retries_and_total_wait_is_bounded(monkeypatch) -> None:
    throttle = Response(429, {"code": "RATE_LIMITED", "detail": {
        "auth_admission_retry": True, "retry_after_seconds": 150,
    }})
    transport = Transport([throttle] * 4)
    waits = []
    monkeypatch.setattr(admin_write_client.time, "sleep", waits.append)
    with pytest.raises(RuntimeError, match="429"):
        client(transport).create_app({"name": "probe", "dept": "test"})
    assert waits == [150, 150] and len(transport.calls) == 3
    assert all(call[1].endswith("/step-up") for call in transport.calls)



@pytest.mark.parametrize("delay", [45, 300])
def test_server_admission_window_is_honored_before_single_mutation(delay, monkeypatch) -> None:
    busy = Response(429, {"code": "RATE_LIMITED", "detail": {
        "auth_admission_retry": True, "retry_after_seconds": delay,
    }})
    transport = Transport([busy, Response(200, {"token": "grant"}), Response(200, {})])
    waits = []
    monkeypatch.setattr(admin_write_client.time, "sleep", waits.append)
    assert client(transport).create_app({"name": "probe", "dept": "test"}).status == 200
    assert waits == [delay]
    assert [call[1] for call in transport.calls] == [
        "/api/v1/web/admin/step-up", "/api/v1/web/admin/step-up", "/api/v1/web/admin/apps",
    ]


@pytest.mark.parametrize("delay", [0, 301, True, "45"])
def test_invalid_admission_delay_never_retries_or_writes(delay, monkeypatch) -> None:
    transport = Transport([Response(429, {"code": "RATE_LIMITED", "detail": {
        "auth_admission_retry": True, "retry_after_seconds": delay,
    }})])
    waits = []
    monkeypatch.setattr(admin_write_client.time, "sleep", waits.append)
    with pytest.raises(RuntimeError, match="429"):
        client(transport).create_app({"name": "probe", "dept": "test"})
    assert waits == [] and len(transport.calls) == 1


def test_repeated_short_admission_waits_still_stop_after_four_attempts(monkeypatch) -> None:
    busy = Response(429, {"code": "RATE_LIMITED", "detail": {
        "auth_admission_retry": True, "retry_after_seconds": 1,
    }})
    transport = Transport([busy] * 4)
    waits = []
    monkeypatch.setattr(admin_write_client.time, "sleep", waits.append)
    with pytest.raises(RuntimeError, match="429"):
        client(transport).create_app({"name": "probe", "dept": "test"})
    assert waits == [1, 1, 1] and len(transport.calls) == 4
