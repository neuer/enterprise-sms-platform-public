"""验收与压测脚本复用的高风险管理 API 客户端；不绕过真实授权。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol


class HttpResult(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def data(self) -> object: ...


class AdminTransport[T: HttpResult](Protocol):
    def request(
        self, method: str, path: str, *, payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> T: ...


# 仅为公开请求的默认字段，测试与服务器 Pydantic 合同逐字段核对；不含任何凭据。
APP_DEFAULTS: dict[str, Any] = {
    "allowed_categories": ["notice"], "default_sign": None, "daily_quota": 0,
    "rate_limit_per_min": 60, "recipient_limit_per_min": 10_000,
    "segment_limit_per_min": 10_000, "max_in_flight_chunks": 200,
    "allow_market_api_bulk": False, "blacklist_check": True, "freq_override": None,
    "allowed_ips": [], "ip_allowlist_exempt_until": None,
    "unlimited_quota_exempt_until": None, "admission_exempt_note": None,
    "callback_url": None, "callback_report_enabled": False,
}


def complete_app_config(payload: Mapping[str, object], *, create: bool) -> dict[str, Any]:
    """补齐默认字段供授权绑定；拒绝未知字段，不能把一次性凭据带入授权。"""
    defaults = deepcopy(APP_DEFAULTS)
    required = {"name", "dept"} if create else {
        "dept", "allowed_categories", "daily_quota", "rate_limit_per_min", "blacklist_check",
    }
    if not create:
        defaults["status"] = 1
    if not required <= payload.keys() or payload.keys() - (defaults.keys() | required):
        raise ValueError("application request fields are invalid")
    return {**defaults, **payload}


class AdminWriteClient[T: HttpResult]:
    """读取真实版本、申请单次授权、仅调用一次写接口；失败不重放写入。"""

    def __init__(
        self, api: AdminTransport[T], token: str, password: str, *,
        failure: type[RuntimeError], label: str,
    ) -> None:
        self.api = api
        self.headers = {"Authorization": f"Bearer {token}"}
        self.password = password
        self.failure = failure
        self.label = label

    def _fail(self, message: str) -> RuntimeError:
        return self.failure(f"{self.label} {message}")

    def _revision(self, row: Mapping[str, object]) -> str:
        value = row.get("updated_at")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return value
            except ValueError:
                pass
        raise self._fail("current revision is unavailable")

    def _grant(self, operation: str, target: str, parameters: dict[str, Any]) -> dict[str, str]:
        waited = 0
        for attempt in range(4):
            response = self.api.request(
                "POST", "/api/v1/web/admin/step-up", headers=self.headers,
                payload={"operation": operation, "target_id": target,
                         "parameters": parameters, "password": self.password},
            )
            data = response.data if isinstance(response.data, dict) else {}
            if response.status == 200:
                token = data.get("token")
                if isinstance(token, str) and token:
                    return {**self.headers, "X-Admin-Step-Up": token}
                raise self._fail("reauthentication omitted authorization")
            detail = data.get("detail")
            delay = detail.get("retry_after_seconds") if isinstance(detail, dict) else None
            if not (
                response.status == 429 and data.get("code") == "RATE_LIMITED"
                and isinstance(detail, dict) and detail.get("auth_admission_retry") is True
                and type(delay) is int and 1 <= delay <= 30
                and attempt < 3 and waited + delay <= 60
            ):
                raise self._fail(f"reauthentication returned HTTP {response.status}")
            time.sleep(delay)
            waited += delay
        raise self._fail("reauthentication retry bound exceeded")

    def create_app(self, payload: Mapping[str, object]) -> T:
        config = complete_app_config(payload, create=True)
        headers = self._grant("app_create", "new", {"config": config})
        return self.api.request("POST", "/api/v1/web/admin/apps", payload=config, headers=headers)

    def _app_revision(self, app_id: int) -> str:
        if type(app_id) is not int or app_id < 1:
            raise self._fail("application identifier is invalid")
        response = self.api.request("GET", f"/api/v1/web/admin/apps/{app_id}", headers=self.headers)
        if response.status != 200 or not isinstance(response.data, dict):
            raise self._fail(f"application lookup returned HTTP {response.status}")
        return self._revision(response.data)

    def update_app(self, app_id: int, payload: Mapping[str, object]) -> T:
        config = complete_app_config(payload, create=False)
        revision = self._app_revision(app_id)
        headers = self._grant("app_update", str(app_id), {
            "config": config, "expected_updated_at": revision,
        })
        return self.api.request("PUT", f"/api/v1/web/admin/apps/{app_id}", payload=config,
                                headers={**headers, "X-App-Revision": revision})

    def disable_app(self, app_id: int) -> T:
        revision = self._app_revision(app_id)
        headers = self._grant("app_disable", str(app_id), {"expected_updated_at": revision})
        return self.api.request("DELETE", f"/api/v1/web/admin/apps/{app_id}",
                                headers={**headers, "X-App-Revision": revision})

    def update_configs(self, updates: Mapping[str, str | None]) -> T:
        response = self.api.request("GET", "/api/v1/web/admin/configs", headers=self.headers)
        if response.status != 200 or not isinstance(response.data, list):
            raise self._fail(f"configuration lookup returned HTTP {response.status}")
        rows = {row["key"]: row for row in response.data
                if isinstance(row, dict) and isinstance(row.get("key"), str)}
        items: list[dict[str, object]] = []
        if not 1 <= len(updates) <= 50:
            raise self._fail("configuration update size is invalid")
        for key, value in updates.items():
            row = rows.get(key)
            # 本工具只修改验收/压测所需的非凭据参数，不接纳可回显的秘密配置。
            if row is None or row.get("sensitive") is not False or not isinstance(value, str):
                raise self._fail("non-secret configuration is unavailable")
            items.append({"key": key, "value": value,
                          "expected_updated_at": self._revision(row)})
        headers = self._grant("config_update", "default", {"items": items})
        return self.api.request("PUT", "/api/v1/web/admin/configs",
                                payload={"items": items}, headers=headers)
