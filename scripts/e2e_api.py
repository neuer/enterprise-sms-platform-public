#!/usr/bin/env python3
"""在 mock Compose 栈串行执行固定的 20 项 API UAT。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from runtime_credentials import read_secret_file

CASE_IDS = tuple([f"{value:02d}" for value in range(5, 21)] + ["24", "25", "26", "27"])
REQUIRED_APPS = frozenset({"app-iam", "app-oa", "app-mkt"})
SAFE_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_DATABASE_VALUE = re.compile(r"^[A-Za-z0-9_.:@+\-]+$")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class UatFailure(RuntimeError):
    """仅包含用例编号和安全摘要，不携带响应或业务载荷。"""


class CommandFailure(UatFailure):
    def __init__(self, executable: str, returncode: int) -> None:
        super().__init__(f"command failed: {Path(executable).name} rc={returncode}")


class Runner(Protocol):
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes: ...


class CommandRunner:
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty argv sequence")
        argv = [str(item) for item in command]
        result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
        if result.returncode != 0:
            raise CommandFailure(argv[0], result.returncode)
        return result.stdout


class RollbackStack:
    """保存同步恢复动作；无论单项失败与否均按 LIFO 尽力执行。"""

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], object]] = []

    def defer(self, callback: Callable[[], object]) -> None:
        self._callbacks.append(callback)

    def restore(self) -> tuple[str, ...]:
        errors: list[str] = []
        while self._callbacks:
            callback = self._callbacks.pop()
            try:
                callback()
            except Exception as error:  # 恢复必须继续执行其余动作
                errors.append(type(error).__name__)
        return tuple(errors)


def wait_until[T](
    case_id: str,
    predicate: Callable[[], T | None],
    *,
    timeout_s: float,
    interval_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    if timeout_s <= 0 or interval_s <= 0:
        raise ValueError("wait bounds must be positive")
    deadline = clock() + timeout_s
    while clock() <= deadline:
        value = predicate()
        if value is not None:
            return value
        sleeper(interval_s)
    raise UatFailure(f"UAT-{case_id} timeout")


def closed_market_window(now: datetime) -> tuple[str, datetime]:
    """构造当前时刻之外的短窗口，并返回下一次窗口起点。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market window clock must be timezone-aware")
    local = now.astimezone(SHANGHAI)
    minute_of_day = local.hour * 60 + local.minute
    if minute_of_day <= 1436:
        start_minute = minute_of_day + 2
        expected_date = local.date()
    else:
        start_minute = 0
        expected_date = (local + timedelta(days=1)).date()
    end_minute = start_minute + 1
    window = (
        f"{start_minute // 60:02d}:{start_minute % 60:02d}-"
        f"{end_minute // 60:02d}:{end_minute % 60:02d}"
    )
    expected = datetime(
        expected_date.year,
        expected_date.month,
        expected_date.day,
        start_minute // 60,
        start_minute % 60,
        tzinfo=SHANGHAI,
    )
    return window, expected


def parse_stat_snapshot(raw: str) -> tuple[tuple[str, int, int, int, int, int], ...]:
    """解析只含日期与聚合整数的 stat_daily 安全快照。"""

    if not raw:
        return ()
    rows: list[tuple[str, int, int, int, int, int]] = []
    for encoded in raw.split(";"):
        fields = encoded.split("|")
        if len(fields) != 6 or re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[0]) is None:
            raise UatFailure("invalid stat snapshot")
        values = [int(value) for value in fields[1:]]
        if any(value < 0 for value in values):
            raise UatFailure("invalid stat snapshot")
        rows.append((fields[0], values[0], values[1], values[2], values[3], values[4]))
    return tuple(rows)


def load_keys(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("API key file is unavailable") from error
    if not isinstance(document, dict):
        raise ValueError("API key file must be an object")
    keys = {str(name): value for name, value in document.items() if isinstance(value, str)}
    if not REQUIRED_APPS.issubset(keys) or any(not keys[name] for name in REQUIRED_APPS):
        raise ValueError("API keys are incomplete")
    return keys


def verify_callback_signature(
    secret: str,
    *,
    raw_body: str,
    timestamp: str,
    signature: str,
    now_s: int,
) -> bool:
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now_s - sent_at) > 300:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{raw_body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    data: object


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class HttpClient:
    def __init__(self, base_url: str, *, timeout_s: float = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        body = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
                return HttpResponse(response.status, json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                data = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                data = None
            return HttpResponse(error.code, data)
        except (OSError, TimeoutError) as error:
            raise UatFailure("HTTP transport failed") from error


class ComposeProbe:
    """只运行 Compose argv，并向用例返回安全的标量事实。"""

    def __init__(
        self,
        runner: Runner,
        *,
        compose_file: Path,
        repository_root: Path,
    ) -> None:
        self.runner = runner
        self.compose_file = compose_file
        self.repository_root = repository_root

    def _compose(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "--profile",
            "dev",
            *arguments,
        ]

    def psql_value(self, sql: str, **variables: str) -> str:
        rendered = sql
        for name, value in variables.items():
            token = f":'{name}'"
            if (
                SAFE_VARIABLE.fullmatch(name) is None
                or SAFE_DATABASE_VALUE.fullmatch(value) is None
                or len(value) > 128
                or token not in rendered
            ):
                raise ValueError("invalid database probe variable")
            rendered = rendered.replace(token, f"'{value}'")
        if re.search(r":'[A-Za-z_][A-Za-z0-9_]*'", rendered):
            raise ValueError("unbound database probe variable")
        output = self.runner.run(
            self._compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "sms_owner",
                "-d",
                "sms",
                "-Atc",
                rendered,
            ),
            cwd=self.repository_root,
        )
        return output.decode("utf-8", errors="strict").strip()

    def psql_execute(self, sql: str, **variables: str) -> None:
        self.psql_value(sql, **variables)

    def psql_count(self, sql: str, **variables: str) -> int:
        if re.match(r"^\s*SELECT\s+count\(\*\)\s+FROM\b", sql, re.IGNORECASE) is None:
            raise ValueError("database probe must be count-only")
        value = self.psql_value(sql, **variables)
        if not value.isdecimal():
            raise UatFailure("UAT database probe returned invalid count")
        return int(value)

    def start_beat(self) -> None:
        self.runner.run(self._compose("start", "beat"), cwd=self.repository_root)

    def stop_beat(self, rollback: RollbackStack) -> None:
        rollback.defer(self.start_beat)
        self.runner.run(self._compose("stop", "beat"), cwd=self.repository_root)

    def redis_int(self, key: str) -> int:
        if not key or any(character.isspace() for character in key):
            raise ValueError("invalid Redis probe key")
        output = self.runner.run(
            self._compose(
                "exec",
                "-T",
                "redis-control",
                "sh",
                "-ec",
                (
                    'exec redis-cli --user sms_control --askpass --raw GET "$1" '
                    "< /run/secrets/redis_control_password"
                ),
                "sh",
                key,
            ),
            cwd=self.repository_root,
        )
        value = output.decode("ascii", errors="strict").strip()
        if value in {"", "(nil)"}:
            return 0
        if not value.isdecimal():
            raise UatFailure("UAT Redis probe returned invalid count")
        return int(value)


class UatSuite:
    def __init__(
        self,
        api: HttpTransport | None,
        mock: HttpTransport | None,
        keys: Mapping[str, str],
        *,
        mock_password: str = "",
        probe: ComposeProbe | None = None,
        run_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.mock = mock
        self.keys = dict(keys)
        self.mock_password = mock_password
        self.probe = probe
        self.run_id = run_id or uuid4().hex[:10]
        self.clock = clock
        digest = hashlib.sha256(self.run_id.encode("utf-8")).digest()
        self._phone_bucket = int.from_bytes(digest[:2], "big") % 90_000
        self.rollback = RollbackStack()
        self._tokens: dict[str, str] = {}
        self._account_ids: dict[str, int] = {}

    @classmethod
    def stub(cls, *, run_id: str) -> UatSuite:
        return cls(None, None, {}, run_id=run_id)

    def phone(self, namespace: int, index: int) -> str:
        if namespace < 0 or index < 0 or index >= 1_000:
            raise ValueError("invalid phone namespace")
        tail = namespace * 100_000_000 + self._phone_bucket * 1_000 + index
        if tail >= 10_000_000_000:
            raise ValueError("phone namespace exhausted")
        return f"1{tail:010d}"

    @staticmethod
    def _object(value: object, case_id: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise UatFailure(f"UAT-{case_id} invalid response shape")
        return value

    @staticmethod
    def _code(response: HttpResponse) -> str | None:
        if isinstance(response.data, dict):
            value = response.data.get("code")
            if isinstance(value, str):
                return value
        return None

    def _expect(
        self,
        case_id: str,
        response: HttpResponse,
        status: int,
        *,
        code: str | None = None,
    ) -> dict[str, Any]:
        actual_code = self._code(response)
        if response.status != status or (code is not None and actual_code != code):
            suffix = f" code={actual_code}" if actual_code is not None else ""
            raise UatFailure(f"UAT-{case_id} expected HTTP {status}, got {response.status}{suffix}")
        if response.data is None:
            return {}
        return self._object(response.data, case_id)

    def _request(
        self,
        client: HttpTransport | None,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if client is None:
            raise UatFailure("UAT client is unavailable")
        return client.request(method, path, payload=payload, headers=headers)

    def login(self, username: str, *, refresh: bool = False) -> str:
        if not refresh and username in self._tokens:
            return self._tokens[username]
        response = self._request(
            self.api,
            "POST",
            "/api/v1/web/auth/login",
            payload={
                "provider_code": "ad",
                "username": username,
                "password": self.mock_password,
            },
        )
        data = self._expect("00", response, 200)
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise UatFailure("UAT-00 login omitted token")
        user = data.get("user")
        if not isinstance(user, dict):
            raise UatFailure("UAT-00 login omitted user")
        account_id = user.get("account_id")
        if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id < 1:
            raise UatFailure("UAT-00 login omitted account id")
        self._tokens[username] = token
        self._account_ids[username] = account_id
        return token

    def _bearer(self, username: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.login(username)}"}

    def _api_key(self, app: str) -> dict[str, str]:
        key = self.keys.get(app)
        if not key:
            raise UatFailure("UAT API key is unavailable")
        return {"X-Api-Key": key}

    def api_send(
        self,
        case_id: str,
        *,
        app: str,
        category: str,
        mobiles: Sequence[str],
        content: str,
        biz_suffix: str,
    ) -> HttpResponse:
        return self._request(
            self.api,
            "POST",
            "/api/v1/messages/send",
            payload={
                "category": category,
                "mobiles": list(mobiles),
                "content": content,
                "biz_id": f"u{case_id}-{self.run_id}-{biz_suffix}"[:32],
            },
            headers=self._api_key(app),
        )

    def web_send(
        self,
        *,
        category: str,
        mobiles: Sequence[str],
        content: str,
        consent_confirmed: bool,
        is_test: bool,
    ) -> HttpResponse:
        return self._request(
            self.api,
            "POST",
            "/api/v1/web/messages/send",
            payload={
                "category": category,
                "mobiles": list(mobiles),
                "content": content,
                "consent_confirmed": consent_confirmed,
                "is_test": is_test,
                "remark": f"uat-{self.run_id}",
            },
            headers=self._bearer("operator01"),
        )

    def mock_state(self) -> dict[str, Any]:
        response = self._request(self.mock, "GET", "/_mock/state")
        return self._expect("00", response, 200)

    def _send_calls(self, batch_no: str) -> list[dict[str, Any]]:
        calls = self.mock_state().get("send_calls")
        if not isinstance(calls, list):
            raise UatFailure("UAT-00 mock state omitted send calls")
        prefix = batch_no[:24]
        return [
            item
            for item in calls
            if isinstance(item, dict)
            and isinstance(item.get("customId"), str)
            and item["customId"].startswith(prefix)
        ]

    def wait_send(self, case_id: str, batch_no: str, *, timeout_s: float = 15) -> dict[str, Any]:
        def first_call() -> dict[str, Any] | None:
            calls = self._send_calls(batch_no)
            return calls[0] if calls else None

        return wait_until(
            case_id,
            first_call,
            timeout_s=timeout_s,
        )

    def _config_values(self) -> dict[str, str | None]:
        response = self._request(
            self.api,
            "GET",
            "/api/v1/web/admin/configs",
            headers=self._bearer("admin01"),
        )
        if response.status != 200 or not isinstance(response.data, list):
            raise UatFailure("UAT config snapshot failed")
        return {
            str(item["key"]): item.get("value")
            for item in response.data
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }

    def set_config(self, key: str, value: str) -> str | None:
        snapshot = self._config_values()
        previous = snapshot.get(key)
        if key not in snapshot:
            raise UatFailure("UAT config key is unavailable")

        def restore() -> None:
            self._put_config(key, previous)

        self.rollback.defer(restore)
        self._put_config(key, value)
        return previous

    def _put_config(self, key: str, value: str | None) -> None:
        response = self._request(
            self.api,
            "PUT",
            "/api/v1/web/admin/configs",
            payload={"items": [{"key": key, "value": value}]},
            headers=self._bearer("admin01"),
        )
        if response.status != 200:
            raise UatFailure(f"UAT config update failed HTTP {response.status}")

    def _approval(self, case_id: str, batch_no: str) -> dict[str, Any]:
        response = self._request(
            self.api,
            "GET",
            "/api/v1/web/approvals?status=pending&page=1",
            headers=self._bearer("admin01"),
        )
        page = self._expect(case_id, response, 200)
        items = page.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("batch_no") == batch_no:
                    return item
        raise UatFailure(f"UAT-{case_id} approval missing")

    def _set_role(self, role: str, role_override: bool) -> None:
        account_id = self._account_ids.get("operator01")
        if account_id is None:
            self.login("operator01", refresh=True)
            account_id = self._account_ids.get("operator01")
        if account_id is None:
            raise UatFailure("UAT operator account id is unavailable")
        response = self._request(
            self.api,
            "PUT",
            f"/api/v1/web/admin/users/{account_id}/role",
            payload={"role": role, "role_override": role_override},
            headers=self._bearer("admin01"),
        )
        if response.status != 200:
            raise UatFailure(f"UAT role update failed HTTP {response.status}")
        self._tokens.pop("operator01", None)

    def _operator_role(self) -> tuple[str, bool]:
        response = self._request(
            self.api,
            "GET",
            "/api/v1/web/admin/users?keyword=operator01&page=1&page_size=20",
            headers=self._bearer("admin01"),
        )
        page = self._expect("11", response, 200)
        items = page.get("items")
        if not isinstance(items, list):
            raise UatFailure("UAT-11 operator snapshot missing")
        for item in items:
            if isinstance(item, dict) and item.get("username") == "operator01":
                role = item.get("role")
                override = item.get("role_override")
                if isinstance(role, str) and isinstance(override, bool):
                    return role, override
        raise UatFailure("UAT-11 operator snapshot missing")

    def _trigger_job(self, case_id: str, job_name: str) -> None:
        response = self._request(
            self.api,
            "POST",
            f"/api/v1/web/admin/jobs/{urllib.parse.quote(job_name)}/trigger",
            headers=self._bearer("admin01"),
        )
        self._expect(case_id, response, 202)

    def _mock_config(self, case_id: str, payload: Mapping[str, object]) -> dict[str, Any]:
        response = self._request(self.mock, "POST", "/_mock/state", payload=dict(payload))
        return self._expect(case_id, response, 200)

    def _cleanup_http(
        self,
        case_id: str,
        method: str,
        path: str,
        *,
        client: HttpClient | None = None,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
        allowed: tuple[int, ...] = (200,),
    ) -> None:
        response = self._request(
            self.api if client is None else client,
            method,
            path,
            payload=payload,
            headers=headers,
        )
        if response.status not in allowed:
            raise UatFailure(f"UAT-{case_id} cleanup HTTP {response.status}")

    def _force_resume_and_verify_unpaused(self, case_id: str) -> None:
        self._expect(
            case_id,
            self._request(
                self.api,
                "POST",
                "/api/v1/web/admin/queue/resume?force=true",
                headers=self._bearer("admin01"),
            ),
            200,
        )
        status = self._expect(
            case_id,
            self._request(
                self.api,
                "GET",
                "/api/v1/web/admin/queue/status",
                headers=self._bearer("admin01"),
            ),
            200,
        )
        if status.get("realtime_code") is not None or status.get("bulk_code") is not None:
            raise UatFailure(f"UAT-{case_id} queue pause cleanup failed")

    def _alerts(self, case_id: str, alert_type: str) -> list[dict[str, Any]]:
        response = self._request(
            self.api,
            "GET",
            "/api/v1/web/admin/alerts?"
            + urllib.parse.urlencode({"alert_type": alert_type, "page": 1, "page_size": 100}),
            headers=self._bearer("admin01"),
        )
        page = self._expect(case_id, response, 200)
        items = page.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def wait_batch_status(
        self,
        case_id: str,
        batch_no: str,
        expected: str,
        *,
        app: str | None = None,
        timeout_s: float = 20,
    ) -> dict[str, Any]:
        def selected() -> dict[str, Any] | None:
            batch = self._batch(case_id, batch_no, app=app)
            return batch if batch.get("status") == expected else None

        return wait_until(case_id, selected, timeout_s=timeout_s, interval_s=0.5)

    def _batch(self, case_id: str, batch_no: str, *, app: str | None = None) -> dict[str, Any]:
        headers = self._api_key(app) if app is not None else self._bearer("admin01")
        response = self._request(
            self.api,
            "GET",
            f"/api/v1/messages/batches/{urllib.parse.quote(batch_no)}",
            headers=headers,
        )
        return self._expect(case_id, response, 200)

    def _quota_key(self, app_id: int, date_key: str) -> str:
        return f"quota:app:{app_id}:{date_key}"

    def _today(self) -> str:
        return datetime.now(UTC).astimezone(SHANGHAI).strftime("%Y%m%d")

    def _probe(self) -> ComposeProbe:
        if self.probe is None:
            raise UatFailure("UAT Compose probe is unavailable")
        return self.probe

    def _wait_send_pipeline_idle(self, case_id: str) -> None:
        def pending() -> bool | None:
            count = self._probe().psql_count(
                "SELECT count(*) FROM sms_batch b "
                "WHERE b.status = 'queued' "
                "OR EXISTS ("
                "SELECT 1 FROM sms_chunk c "
                "WHERE c.batch_id = b.id "
                "AND c.status IN ('pending','submitting','retrying')"
                ")"
            )
            return True if count == 0 else None

        wait_until(case_id, pending, timeout_s=30, interval_s=0.5)

    def case_05(self) -> None:
        phone = self.phone(5, 0)
        first = self._expect(
            "05",
            self.api_send(
                "05",
                app="app-oa",
                category="notice",
                mobiles=[phone],
                content="幂等验收通知",
                biz_suffix="same",
            ),
            200,
        )
        second = self._expect(
            "05",
            self.api_send(
                "05",
                app="app-oa",
                category="notice",
                mobiles=[phone],
                content="幂等验收通知",
                biz_suffix="same",
            ),
            200,
        )
        batch_no = first.get("batch_no")
        if not isinstance(batch_no, str) or second.get("batch_no") != batch_no:
            raise UatFailure("UAT-05 idempotent batch mismatch")
        if first.get("idempotent") is not False or second.get("idempotent") is not True:
            raise UatFailure("UAT-05 idempotent flags mismatch")
        self.wait_send("05", batch_no)
        if len(self._send_calls(batch_no)) != 1:
            raise UatFailure("UAT-05 duplicate vendor send")

    def case_06(self) -> None:
        response = self.api_send(
            "06",
            app="app-iam",
            category="market",
            mobiles=[self.phone(6, 0)],
            content="类别越权验收回T退订",
            biz_suffix="deny",
        )
        self._expect("06", response, 403, code="CATEGORY_NOT_ALLOWED")

    def case_07(self) -> None:
        shared = self.phone(7, 0)
        self._expect(
            "07",
            self.api_send(
                "07",
                app="app-iam",
                category="verify",
                mobiles=[shared],
                content="验证码123456",
                biz_suffix="first",
            ),
            200,
        )
        second = self._expect(
            "07",
            self.api_send(
                "07",
                app="app-iam",
                category="verify",
                mobiles=[shared, self.phone(7, 1)],
                content="验证码654321",
                biz_suffix="second",
            ),
            200,
        )
        if second.get("accepted") != 1 or second.get("removed_freq_limit") != 1:
            raise UatFailure("UAT-07 frequency removal mismatch")

    def case_08(self) -> None:
        now = datetime.now(UTC).astimezone(SHANGHAI)
        window, expected_start = closed_market_window(now)
        previous_window = self.set_config("market_send_window", window)
        data = self._expect(
            "08",
            self.api_send(
                "08",
                app="app-mkt",
                category="market",
                mobiles=[self.phone(8, 0)],
                content="营销窗验收回T退订",
                biz_suffix="window",
            ),
            200,
        )
        batch_no = data.get("batch_no")
        scheduled_at = data.get("scheduled_at")
        try:
            scheduled = (
                datetime.fromisoformat(scheduled_at) if isinstance(scheduled_at, str) else None
            )
        except ValueError:
            scheduled = None
        if (
            data.get("status") != "scheduled"
            or data.get("deferred_reason") != "market_window"
            or scheduled is None
            or scheduled.tzinfo is None
            or abs((scheduled - expected_start).total_seconds()) > 1
            or scheduled <= now
            or not isinstance(batch_no, str)
        ):
            raise UatFailure("UAT-08 market deferral mismatch")
        cancelled = self._request(
            self.api,
            "POST",
            f"/api/v1/messages/batches/{urllib.parse.quote(batch_no)}/cancel",
            headers=self._api_key("app-mkt"),
        )
        self._expect("08", cancelled, 200)
        self._put_config("market_send_window", previous_window)

    def case_09(self) -> None:
        source_content = "营" * 70
        data = self._expect(
            "09",
            self.web_send(
                category="market",
                mobiles=[self.phone(9, 0)],
                content=source_content,
                consent_confirmed=True,
                is_test=True,
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if not isinstance(batch_no, str) or data.get("est_segments") != 2:
            raise UatFailure("UAT-09 acceptance mismatch")
        call = self.wait_send("09", batch_no)
        if call.get("content") != source_content + "回T退订":
            raise UatFailure("UAT-09 unsubscribe suffix missing")

    def case_10(self) -> None:
        denied = self.web_send(
            category="market",
            mobiles=[self.phone(10, 0)],
            content="营销同意验收回T退订",
            consent_confirmed=False,
            is_test=True,
        )
        self._expect("10", denied, 422, code="CONSENT_REQUIRED")
        accepted = self._expect(
            "10",
            self.web_send(
                category="market",
                mobiles=[self.phone(10, 1)],
                content="营销同意验收回T退订",
                consent_confirmed=True,
                is_test=True,
            ),
            200,
        )
        batch_no = accepted.get("batch_no")
        if not isinstance(batch_no, str):
            raise UatFailure("UAT-10 batch missing")
        response = self._request(
            self.api,
            "GET",
            "/api/v1/web/admin/audit-logs?action=message_send&page=1&page_size=100",
            headers=self._bearer("admin01"),
        )
        page = self._expect("10", response, 200)
        items = page.get("items")
        matched = (
            [
                item
                for item in items
                if isinstance(items, list)
                and isinstance(item, dict)
                and item.get("object_id") == batch_no
            ]
            if isinstance(items, list)
            else []
        )
        if not matched or not isinstance(matched[0].get("after_val"), dict):
            raise UatFailure("UAT-10 consent audit missing")
        after = matched[0]["after_val"]
        serialized_after = json.dumps(after, ensure_ascii=False, sort_keys=True)
        if (
            set(after) != {"batch_no", "phone_count", "consent_confirmed"}
            or after.get("batch_no") != batch_no
            or after.get("phone_count") != 1
            or after.get("consent_confirmed") is not True
            or re.search(r"(?<!\d)1\d{10}(?!\d)", serialized_after) is not None
        ):
            raise UatFailure("UAT-10 consent audit mismatch")

    def case_11(self) -> None:
        data = self._expect(
            "11",
            self.web_send(
                category="market",
                mobiles=[self.phone(11, index) for index in range(60)],
                content="审批阈值验收回T退订",
                consent_confirmed=True,
                is_test=False,
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if data.get("status") != "pending_approval" or not isinstance(batch_no, str):
            raise UatFailure("UAT-11 approval threshold mismatch")
        approval = self._approval("11", batch_no)
        approval_id = approval.get("id")
        if not isinstance(approval_id, int):
            raise UatFailure("UAT-11 approval id missing")

        original_role, original_override = self._operator_role()
        self.rollback.defer(lambda: self._set_role(original_role, original_override))
        self._set_role("approver", True)
        self.login("operator01", refresh=True)
        self_response = self._request(
            self.api,
            "POST",
            f"/api/v1/web/approvals/{approval_id}/decision",
            payload={"action": "approve", "reason": None},
            headers=self._bearer("operator01"),
        )
        self._expect("11", self_response, 403, code="SELF_APPROVAL_DENIED")
        self._set_role(original_role, original_override)
        approved = self._request(
            self.api,
            "POST",
            f"/api/v1/web/approvals/{approval_id}/decision",
            payload={"action": "approve", "reason": None},
            headers=self._bearer("approver01"),
        )
        self._expect("11", approved, 200)

    def case_12(self) -> None:
        self.set_config("market_approval_threshold", "1")
        quota_key = self._quota_key(0, self._today())
        before = self._probe().redis_int(quota_key)
        data = self._expect(
            "12",
            self.web_send(
                category="market",
                mobiles=[self.phone(12, 0)],
                content="审批过期验收回T退订",
                consent_confirmed=True,
                is_test=False,
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if data.get("status") != "pending_approval" or not isinstance(batch_no, str):
            raise UatFailure("UAT-12 pending approval missing")
        quota_cost = data.get("quota_cost")
        if quota_cost != 1:
            raise UatFailure("UAT-12 quota reservation mismatch")

        def quota_reserved() -> bool | None:
            return (
                True
                if self._probe().redis_int(quota_key) == before + quota_cost
                else None
            )

        wait_until("12", quota_reserved, timeout_s=15, interval_s=0.25)

        self._probe().psql_execute(
            "UPDATE approval SET expires_at=now()+interval '5 seconds' "
            "WHERE batch_id=(SELECT id FROM sms_batch "
            "WHERE batch_no=CAST(:'batch_no' AS char(32))) "
            "AND status='pending'",
            batch_no=batch_no,
        )
        time.sleep(6)
        self._trigger_job("12", "expire_approvals")

        def expired() -> dict[str, Any] | None:
            batch = self._batch("12", batch_no)
            return batch if batch.get("status") == "expired" else None

        wait_until("12", expired, timeout_s=15, interval_s=0.5)

        def quota_refunded() -> bool | None:
            return True if self._probe().redis_int(quota_key) == before else None

        wait_until("12", quota_refunded, timeout_s=15, interval_s=0.25)

        def expiration_alert() -> bool | None:
            return (
                True
                if any(
                    isinstance(item.get("detail"), dict)
                    and item["detail"].get("batch_no") == batch_no
                    for item in self._alerts("12", "approval_expired")
                )
                else None
            )

        wait_until("12", expiration_alert, timeout_s=15, interval_s=0.25)

    def case_13(self) -> None:
        app_id_raw = self._probe().psql_value("SELECT id FROM app WHERE name='app-oa'")
        if not app_id_raw.isdecimal():
            raise UatFailure("UAT-13 app id unavailable")
        quota_key = self._quota_key(int(app_id_raw), self._today())
        before = self._probe().redis_int(quota_key)
        data = self._expect(
            "13",
            self.api_send(
                "13",
                app="app-oa",
                category="notice",
                mobiles=[self.phone(13, index) for index in range(100)],
                content="计" * 150,
                biz_suffix="segments",
            ),
            200,
        )
        if data.get("est_segments") != 3 or data.get("quota_cost") != 300:
            raise UatFailure("UAT-13 billing mismatch")
        if self._probe().redis_int(quota_key) - before != 300:
            raise UatFailure("UAT-13 quota delta mismatch")

    def case_14(self) -> None:
        blocked_phone = self.phone(14, 0)
        added = self._expect(
            "14",
            self._request(
                self.api,
                "POST",
                "/api/v1/web/admin/blacklist",
                payload={"phones": [blocked_phone], "source": "manual", "remark": "uat"},
                headers=self._bearer("admin01"),
            ),
            200,
        )
        blacklist_items = added.get("items")
        if not isinstance(blacklist_items, list) or not blacklist_items:
            raise UatFailure("UAT-14 blacklist creation failed")
        phone_hmac = blacklist_items[0].get("phone_hmac")
        if not isinstance(phone_hmac, str):
            raise UatFailure("UAT-14 blacklist reference missing")
        self.rollback.defer(
            lambda: self._cleanup_http(
                "14",
                "DELETE",
                f"/api/v1/web/admin/blacklist/{urllib.parse.quote(phone_hmac)}",
                headers=self._bearer("admin01"),
                allowed=(204,),
            )
        )
        filtered = self._expect(
            "14",
            self.api_send(
                "14",
                app="app-oa",
                category="notice",
                mobiles=[blocked_phone, self.phone(14, 1)],
                content="黑名单验收通知",
                biz_suffix="blacklist",
            ),
            200,
        )
        if filtered.get("accepted") != 1 or filtered.get("removed_blacklist") != 1:
            raise UatFailure("UAT-14 blacklist removal mismatch")

        word = f"敏感验收{self.run_id}"
        created_response = self._request(
            self.api,
            "POST",
            "/api/v1/web/admin/sensitive-words",
            payload={"words": [word]},
            headers=self._bearer("admin01"),
        )
        if created_response.status != 200 or not isinstance(created_response.data, list):
            raise UatFailure("UAT-14 sensitive word creation failed")
        created = created_response.data
        word_id = created[0].get("id") if created and isinstance(created[0], dict) else None
        if not isinstance(word_id, int):
            raise UatFailure("UAT-14 sensitive word reference missing")
        self.rollback.defer(
            lambda: self._cleanup_http(
                "14",
                "DELETE",
                f"/api/v1/web/admin/sensitive-words/{word_id}",
                headers=self._bearer("admin01"),
                allowed=(204,),
            )
        )
        sensitive = self.api_send(
            "14",
            app="app-oa",
            category="notice",
            mobiles=[self.phone(14, 2)],
            content=f"通知{word}",
            biz_suffix="sensitive",
        )
        self._expect("14", sensitive, 422, code="SENSITIVE_WORD")

    def case_15(self) -> None:
        app_id_raw = self._probe().psql_value("SELECT id FROM app WHERE name='app-oa'")
        if not app_id_raw.isdecimal():
            raise UatFailure("UAT-15 app id unavailable")
        quota_key = self._quota_key(int(app_id_raw), self._today())
        before = self._probe().redis_int(quota_key)
        scheduled_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        response = self._request(
            self.api,
            "POST",
            "/api/v1/messages/send",
            payload={
                "category": "notice",
                "mobiles": [self.phone(15, 0)],
                "content": "定时取消验收",
                "scheduled_at": scheduled_at,
                "biz_id": f"u15-{self.run_id}",
            },
            headers=self._api_key("app-oa"),
        )
        data = self._expect("15", response, 200)
        batch_no = data.get("batch_no")
        if data.get("status") != "scheduled" or not isinstance(batch_no, str):
            raise UatFailure("UAT-15 scheduled batch missing")
        cancel = self._request(
            self.api,
            "POST",
            f"/api/v1/messages/batches/{urllib.parse.quote(batch_no)}/cancel",
            headers=self._api_key("app-oa"),
        )
        self._expect("15", cancel, 200)
        if self._batch("15", batch_no, app="app-oa").get("status") != "cancelled":
            raise UatFailure("UAT-15 batch was not cancelled")

        def quota_refunded() -> bool | None:
            return True if self._probe().redis_int(quota_key) == before else None

        wait_until("15", quota_refunded, timeout_s=15, interval_s=0.25)

    def case_16(self) -> None:
        self._wait_send_pipeline_idle("16/precondition")
        initial_mock = self.mock_state()
        initial_balance = initial_mock.get("balance")
        initial_latency = initial_mock.get("latency_ms")
        initial_code = initial_mock.get("next_send_code")
        initial_times = initial_mock.get("next_send_times")
        if not isinstance(initial_balance, int) or not isinstance(initial_latency, int):
            raise UatFailure("UAT-16 mock snapshot missing")
        initial_queue = self._expect(
            "16",
            self._request(
                self.api,
                "GET",
                "/api/v1/web/admin/queue/status",
                headers=self._bearer("admin01"),
            ),
            200,
        )
        if (
            initial_queue.get("realtime_code") is not None
            or initial_queue.get("bulk_code") is not None
        ):
            raise UatFailure("UAT-16 requires unpaused queues")

        def restore_balance_fault() -> None:
            payload: dict[str, object] = {
                "balance": initial_balance,
                "latency_ms": initial_latency,
            }
            if (
                isinstance(initial_code, int)
                and isinstance(initial_times, int)
                and initial_times > 0
            ):
                payload |= {"next_send_code": initial_code, "times": initial_times}
            else:
                payload["clear_send_error"] = True
            self._mock_config("16", payload)
            self._trigger_job("16", "poll_balance")

            def balance_restored() -> bool | None:
                current = self._expect(
                    "16",
                    self._request(
                        self.api,
                        "GET",
                        "/api/v1/web/admin/queue/status",
                        headers=self._bearer("admin01"),
                    ),
                    200,
                )
                return True if current.get("balance") == initial_balance else None

            wait_until("16", balance_restored, timeout_s=15, interval_s=0.5)
            self._force_resume_and_verify_unpaused("16")

        self.rollback.defer(restore_balance_fault)
        self._mock_config("16", {"next_send_code": 999, "times": 1})
        data = self._expect(
            "16",
            self.api_send(
                "16",
                app="app-oa",
                category="notice",
                mobiles=[self.phone(16, 0)],
                content="余额熔断验收",
                biz_suffix="balance",
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if not isinstance(batch_no, str):
            raise UatFailure("UAT-16 batch missing")
        self.wait_batch_status(
            "16/balance-blocked",
            batch_no,
            "balance_blocked",
            app="app-oa",
        )
        status = self._expect(
            "16",
            self._request(
                self.api,
                "GET",
                "/api/v1/web/admin/queue/status",
                headers=self._bearer("admin01"),
            ),
            200,
        )
        if status.get("realtime_code") != "999" or status.get("bulk_code") != "999":
            raise UatFailure("UAT-16 queue pause mismatch")
        if not any(item.get("level") == "crit" for item in self._alerts("16", "balance_blocked")):
            raise UatFailure("UAT-16 critical alert missing")
        self._mock_config("16", {"balance": 1_000_000, "latency_ms": 0})
        self._trigger_job("16", "poll_balance")

        def balance_ready() -> dict[str, Any] | None:
            current = self._expect(
                "16",
                self._request(
                    self.api,
                    "GET",
                    "/api/v1/web/admin/queue/status",
                    headers=self._bearer("admin01"),
                ),
                200,
            )
            return current if current.get("balance") == 1_000_000 else None

        wait_until(
            "16/balance-ready",
            balance_ready,
            timeout_s=15,
            interval_s=0.5,
        )
        resumed = self._request(
            self.api,
            "POST",
            "/api/v1/web/admin/queue/resume",
            headers=self._bearer("admin01"),
        )
        result = self._expect("16", resumed, 200)
        if result.get("resumed_batches", 0) < 1:
            raise UatFailure("UAT-16 no batch resumed")
        self.wait_send("16/resumed-send", batch_no, timeout_s=15)
        if len(self._send_calls(batch_no)) != 1:
            raise UatFailure("UAT-16 resumed batch was not submitted once")

    def case_17(self) -> None:
        self._wait_send_pipeline_idle("17")
        initial_latency = self.mock_state().get("latency_ms")
        if not isinstance(initial_latency, int):
            raise UatFailure("UAT-17 mock snapshot missing")
        self._mock_config("17", {"latency_ms": 12_000})
        self.rollback.defer(lambda: self._mock_config("17", {"latency_ms": initial_latency}))
        data = self._expect(
            "17",
            self.api_send(
                "17",
                app="app-iam",
                category="verify",
                mobiles=[self.phone(17, 0)],
                content="验证码246810",
                biz_suffix="uncertain",
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if not isinstance(batch_no, str):
            raise UatFailure("UAT-17 batch missing")

        def uncertain() -> str | None:
            value = self._probe().psql_value(
                "SELECT c.status FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id "
                "WHERE b.batch_no=CAST(:'batch_no' AS char(32))",
                batch_no=batch_no,
            )
            return value if value == "uncertain" else None

        wait_until("17", uncertain, timeout_s=20, interval_s=0.5)
        self._mock_config("17", {"latency_ms": 0})
        call = self.wait_send("17", batch_no, timeout_s=5)
        custom_id = call.get("customId")
        if not isinstance(custom_id, str) or len(self._send_calls(batch_no)) != 1:
            raise UatFailure("UAT-17 vendor send count mismatch")
        time.sleep(2.2)
        self._trigger_job("17", "poll_report")

        def raw_exists() -> int | None:
            value = self._probe().psql_value(
                "SELECT count(*) FROM raw_vendor_log "
                "WHERE custom_ids @> ARRAY[CAST(:'custom_id' AS text)]",
                custom_id=custom_id,
            )
            return int(value) if value.isdecimal() and int(value) > 0 else None

        wait_until("17", raw_exists, timeout_s=15, interval_s=0.5)
        self._trigger_job("17", "reconcile")

        def submitted() -> str | None:
            value = self._probe().psql_value(
                "SELECT c.status FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id "
                "WHERE b.batch_no=CAST(:'batch_no' AS char(32))",
                batch_no=batch_no,
            )
            return value if value == "submitted" else None

        wait_until("17", submitted, timeout_s=15, interval_s=0.5)
        if len(self._send_calls(batch_no)) != 1:
            raise UatFailure("UAT-17 uncertain batch was resent")

    def case_18(self) -> None:
        suffix = self._phone_bucket % 10_000
        failed_phone = f"1990000{suffix:04d}"
        data = self._expect(
            "18",
            self.api_send(
                "18",
                app="app-oa",
                category="notice",
                mobiles=[failed_phone],
                content="失败重发验收",
                biz_suffix="failed",
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if not isinstance(batch_no, str):
            raise UatFailure("UAT-18 batch missing")
        self.wait_send("18", batch_no)
        time.sleep(2.2)
        self._trigger_job("18", "poll_report")

        def failed() -> dict[str, Any] | None:
            batch = self._batch("18", batch_no, app="app-oa")
            return batch if batch.get("failed") == 1 else None

        wait_until("18", failed, timeout_s=15, interval_s=0.5)
        app_id_raw = self._probe().psql_value("SELECT id FROM app WHERE name='app-oa'")
        if not app_id_raw.isdecimal():
            raise UatFailure("UAT-18 app id unavailable")
        quota_key = self._quota_key(int(app_id_raw), self._today())
        quota_before_resend = self._probe().redis_int(quota_key)
        resent = self._expect(
            "18",
            self._request(
                self.api,
                "POST",
                f"/api/v1/messages/batches/{urllib.parse.quote(batch_no)}/resend-failed",
                headers=self._api_key("app-oa"),
            ),
            200,
        )
        resent_batch_no = resent.get("batch_no")
        if (
            resent.get("resend_of") != batch_no
            or not isinstance(resent_batch_no, str)
            or resent_batch_no == batch_no
            or resent.get("accepted") != 1
            or resent.get("status") != "queued"
        ):
            raise UatFailure("UAT-18 resend linkage mismatch")
        if self._probe().redis_int(quota_key) - quota_before_resend != 1:
            raise UatFailure("UAT-18 resend quota mismatch")
        self.wait_send("18", resent_batch_no)
        if len(self._send_calls(resent_batch_no)) != 1:
            raise UatFailure("UAT-18 resend was not submitted once")

    def case_19(self) -> None:
        content = "尊敬的{1}，验证码{2}"
        created = self._expect(
            "19",
            self._request(
                self.api,
                "POST",
                "/api/v1/web/templates",
                payload={
                    "name": f"UAT模板{self.run_id}",
                    "content": content,
                    "var_specs": [{"pos": 1, "max_len": 10}, {"pos": 2, "max_len": 6}],
                },
                headers=self._bearer("operator01"),
            ),
            200,
        )
        template_id = created.get("id")
        if not isinstance(template_id, int):
            raise UatFailure("UAT-19 template id missing")
        contents = self.mock_state().get("template_contents")
        if not isinstance(contents, list) or "尊敬的{s10}，验证码{s6}" not in contents:
            raise UatFailure("UAT-19 vendor template conversion mismatch")
        self._expect(
            "19",
            self._request(
                self.api,
                "POST",
                f"/api/v1/web/templates/{template_id}/sync",
                headers=self._bearer("operator01"),
            ),
            200,
        )
        valid = self._request(
            self.api,
            "POST",
            "/api/v1/messages/send",
            payload={
                "category": "verify",
                "mobiles": [self.phone(19, 0)],
                "template_id": template_id,
                "template_params": ["用户", "123456"],
                "biz_id": f"u19-{self.run_id}-ok",
            },
            headers=self._api_key("app-iam"),
        )
        self._expect("19", valid, 200)
        invalid = self._request(
            self.api,
            "POST",
            "/api/v1/messages/send",
            payload={
                "category": "verify",
                "mobiles": [self.phone(19, 1)],
                "template_id": template_id,
                "template_params": ["超过十个字符的模板参数值", "123456"],
                "biz_id": f"u19-{self.run_id}-bad",
            },
            headers=self._api_key("app-iam"),
        )
        self._expect("19", invalid, 422, code="TEMPLATE_PARAM_MISMATCH")

    def case_20(self) -> None:
        initial_mock = self.mock_state()
        initial_failures = initial_mock.get("callback_failures")
        initial_status = initial_mock.get("callback_status")
        initial_callback_count = initial_mock.get("callback_count")
        if not all(
            isinstance(value, int)
            for value in (initial_failures, initial_status, initial_callback_count)
        ):
            raise UatFailure("UAT-20 mock snapshot missing")
        assert isinstance(initial_failures, int)
        assert isinstance(initial_status, int)
        assert isinstance(initial_callback_count, int)
        self.rollback.defer(
            lambda: self._mock_config(
                "20",
                {
                    "callback_failures": initial_failures,
                    "callback_status": initial_status,
                    "retain_callback_count": initial_callback_count,
                },
            )
        )
        callback_url = "http://mock-vendor:9028/_mock/callback"
        created = self._expect(
            "20",
            self._request(
                self.api,
                "POST",
                "/api/v1/web/admin/apps",
                payload={
                    "name": f"uat-callback-{self.run_id}",
                    "dept": "平台技术部",
                    "allowed_categories": ["notice"],
                    "daily_quota": 0,
                    "rate_limit_per_min": 100,
                    "blacklist_check": False,
                    "callback_url": callback_url,
                    "callback_report_enabled": False,
                },
                headers=self._bearer("admin01"),
            ),
            200,
        )
        app_id = created.get("id")
        api_key = created.get("api_key")
        callback_secret = created.get("callback_secret")
        if (
            not isinstance(app_id, int)
            or not isinstance(api_key, str)
            or not isinstance(callback_secret, str)
        ):
            raise UatFailure("UAT-20 callback app credentials missing")
        self.rollback.defer(
            lambda: self._cleanup_http(
                "20",
                "DELETE",
                f"/api/v1/web/admin/apps/{app_id}",
                headers=self._bearer("admin01"),
                allowed=(204,),
            )
        )
        self._mock_config(
            "20",
            {"callback_failures": 5, "callback_status": 500},
        )
        response = self._request(
            self.api,
            "POST",
            "/api/v1/messages/send",
            payload={
                "category": "notice",
                "mobiles": [self.phone(20, 0)],
                "content": "回调验收通知",
                "biz_id": f"u20-{self.run_id}",
            },
            headers={"X-Api-Key": api_key},
        )
        data = self._expect("20", response, 200)
        batch_no = data.get("batch_no")
        if not isinstance(batch_no, str):
            raise UatFailure("UAT-20 batch missing")
        self.wait_send("20", batch_no)
        time.sleep(2.2)
        self._trigger_job("20", "poll_report")

        def callback_task() -> dict[str, Any] | None:
            page = self._expect(
                "20",
                self._request(
                    self.api,
                    "GET",
                    f"/api/v1/web/admin/callbacks?app_id={app_id}&page=1",
                    headers=self._bearer("admin01"),
                ),
                200,
            )
            items = page.get("items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("batch_no") == batch_no:
                        return item
            return None

        task = wait_until("20", callback_task, timeout_s=15, interval_s=0.5)
        task_id = task.get("id")
        if not isinstance(task_id, int):
            raise UatFailure("UAT-20 callback task id missing")

        def callback_state() -> dict[str, Any] | None:
            return callback_task()

        def retry_ready(expected_retry_count: int) -> Callable[[], dict[str, Any] | None]:
            def predicate() -> dict[str, Any] | None:
                current = callback_state()
                if current is not None and current.get("retry_count") == expected_retry_count:
                    return current
                return None

            return predicate

        self._trigger_job("20", "dispatch_callbacks")
        for retry_count in range(1, 6):
            wait_until(
                "20",
                retry_ready(retry_count),
                timeout_s=15,
                interval_s=0.25,
            )
            if retry_count < 5:
                self._probe().psql_execute(
                    "UPDATE callback_task SET next_retry_at=now() "
                    "WHERE id=CAST(:'task_id' AS bigint) AND status='retrying'",
                    task_id=str(task_id),
                )
                self._trigger_job("20", "dispatch_callbacks")

        self._mock_config("20", {"callback_failures": 1, "callback_status": 500})
        self._probe().psql_execute(
            "UPDATE callback_task SET next_retry_at=now() "
            "WHERE id=CAST(:'task_id' AS bigint) AND status='retrying'",
            task_id=str(task_id),
        )
        self._trigger_job("20", "dispatch_callbacks")
        wait_until(
            "20",
            lambda: (
                current
                if (current := callback_state()) is not None and current.get("status") == "dead"
                else None
            ),
            timeout_s=15,
            interval_s=0.25,
        )
        wait_until(
            "20",
            lambda: self._alerts("20", "callback_dead") or None,
            timeout_s=15,
            interval_s=0.25,
        )
        self._expect(
            "20",
            self._request(
                self.api,
                "POST",
                f"/api/v1/web/admin/callbacks/{task_id}/retry",
                headers=self._bearer("admin01"),
            ),
            200,
        )
        self._trigger_job("20", "dispatch_callbacks")
        wait_until(
            "20",
            lambda: (
                current
                if (current := callback_state()) is not None and current.get("status") == "done"
                else None
            ),
            timeout_s=15,
            interval_s=0.25,
        )
        callbacks_response = self._request(self.mock, "GET", "/_mock/callbacks")
        if callbacks_response.status != 200 or not isinstance(callbacks_response.data, list):
            raise UatFailure("UAT-20 callback evidence missing")
        callbacks = callbacks_response.data
        received = callbacks[-1] if callbacks and isinstance(callbacks[-1], dict) else None
        if received is None:
            raise UatFailure("UAT-20 callback evidence missing")
        raw_body = received.get("raw_body")
        timestamp = received.get("timestamp")
        signature = received.get("signature")
        if not all(isinstance(value, str) for value in (raw_body, timestamp, signature)):
            raise UatFailure("UAT-20 callback signature fields missing")
        assert (
            isinstance(raw_body, str) and isinstance(timestamp, str) and isinstance(signature, str)
        )
        now_s = int(time.time())
        if not verify_callback_signature(
            callback_secret,
            raw_body=raw_body,
            timestamp=timestamp,
            signature=signature,
            now_s=now_s,
        ):
            raise UatFailure("UAT-20 callback signature invalid")
        if verify_callback_signature(
            callback_secret,
            raw_body=raw_body,
            timestamp=timestamp,
            signature=signature,
            now_s=int(timestamp) + 301,
        ):
            raise UatFailure("UAT-20 stale callback signature accepted")

    def case_24(self) -> None:
        content = "验证码246810"
        data = self._expect(
            "24",
            self.api_send(
                "24",
                app="app-iam",
                category="verify",
                mobiles=[self.phone(24, 0)],
                content=content,
                biz_suffix="otp-mask",
            ),
            200,
        )
        batch_no = data.get("batch_no")
        if not isinstance(batch_no, str):
            raise UatFailure("UAT-24 batch missing")
        vendor_call = self.wait_send("24", batch_no)
        if vendor_call.get("content") != content:
            raise UatFailure("UAT-24 vendor OTP content mismatch")
        protected_count = self._probe().psql_count(
            "SELECT count(*) FROM sms_batch "
            "WHERE batch_no=CAST(:'batch_no' AS char(32)) "
            "AND content LIKE '%******%' AND content !~ '[0-9]{4,8}'",
            batch_no=batch_no,
        )
        detail = self._batch("24", batch_no, app="app-iam")
        stored_content = detail.get("content")
        if (
            protected_count != 1
            or not isinstance(stored_content, str)
            or "******" not in stored_content
            or re.search(r"[0-9]{4,8}", stored_content) is not None
        ):
            raise UatFailure("UAT-24 persisted OTP was not masked")

    def case_25(self) -> None:
        phone = self.phone(25, 0)
        custom_id = f"legacy-{self.run_id}"
        self._mock_config(
            "25",
            {
                "enqueue_report": {
                    "taskId": f"legacy-task-{self.run_id}",
                    "customId": custom_id,
                    "phone": phone,
                    "reportStatus": 1,
                }
            },
        )
        self._trigger_job("25", "poll_report")

        def unmatched_page() -> dict[str, Any] | None:
            response = self._request(
                self.api,
                "GET",
                "/api/v1/web/admin/unmatched-reports?"
                + urllib.parse.urlencode({"phone": phone, "page": 1, "page_size": 20}),
                headers=self._bearer("admin01"),
            )
            page = self._expect("25", response, 200)
            return page if page.get("total") == 1 else None

        page = wait_until("25", unmatched_page, timeout_s=20, interval_s=0.5)
        items = page.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise UatFailure("UAT-25 unmatched report missing")
        item = items[0]
        expected_mask = f"{phone[:3]}****{phone[-4:]}"
        if (
            not isinstance(item, dict)
            or item.get("phone_mask") != expected_mask
            or phone in json.dumps(page, ensure_ascii=False)
        ):
            raise UatFailure("UAT-25 unmatched query exposed invalid phone data")
        if (
            self._probe().psql_count(
                "SELECT count(*) FROM unmatched_report "
                "WHERE custom_id=CAST(:'custom_id' AS varchar(64)) "
                "AND phone_enc IS NOT NULL AND phone_hmac IS NOT NULL "
                "AND phone_mask IS NOT NULL AND key_version IS NOT NULL",
                custom_id=custom_id,
            )
            != 1
        ):
            raise UatFailure("UAT-25 unmatched protection columns missing")
        export = self._expect(
            "25",
            self._request(
                self.api,
                "POST",
                "/api/v1/web/admin/unmatched-reports/export",
                payload={"phone": phone, "decrypted": False},
                headers=self._bearer("admin01"),
            ),
            202,
        )
        task_public_id = export.get("id")
        if (
            not isinstance(task_public_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                task_public_id,
            )
            is None
            or export.get("decrypted") is not False
        ):
            raise UatFailure("UAT-25 ciphertext export task missing")
        if (
            self._probe().psql_count(
                "SELECT count(*) FROM export_task "
                "WHERE public_id=CAST(:'task_public_id' AS uuid) "
                "AND creator_account_id IS NOT NULL AND scope_resolved "
                "AND decrypted=false "
                "AND NOT (filters ? 'phone') "
                "AND jsonb_typeof(filters->'phone_hmacs')='array' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM jsonb_array_elements_text(filters->'phone_hmacs') "
                "AS item(phone_hmac) WHERE phone_hmac !~ '^[0-9a-f]{64}$'"
                ")",
                task_public_id=task_public_id,
            )
            != 1
        ):
            raise UatFailure("UAT-25 export filters retained plaintext phone")

    def case_26(self) -> None:
        self.set_config("anomaly_enabled", "true")
        self.set_config("anomaly_multiplier", "3")
        self.set_config("anomaly_min_total", "500")
        app_id_raw = self._probe().psql_value("SELECT id FROM app WHERE name='app-iam'")
        if not app_id_raw.isdecimal():
            raise UatFailure("UAT-26 app id unavailable")
        baseline_snapshot = parse_stat_snapshot(
            self._probe().psql_value(
                "SELECT COALESCE(string_agg(to_char(stat_date,'YYYY-MM-DD')||'|'||"
                "CAST(total AS text)||'|'||CAST(total_segments AS text)||'|'||"
                "CAST(delivered AS text)||'|'||CAST(failed AS text)||'|'||"
                "CAST(unknown_cnt AS text),';' ORDER BY stat_date),'') "
                "FROM stat_daily WHERE dim_type='app' "
                "AND dim_value=CAST(:'app_id' AS varchar(128)) AND category='verify' "
                "AND stat_date BETWEEN current_date-7 AND current_date-1",
                app_id=app_id_raw,
            )
        )
        baseline_delete = (
            "DELETE FROM stat_daily WHERE dim_type='app' "
            "AND dim_value=CAST(:'app_id' AS varchar(128)) AND category='verify' "
            "AND stat_date BETWEEN current_date-7 AND current_date-1"
        )

        def restore_baseline() -> None:
            self._probe().psql_execute(baseline_delete, app_id=app_id_raw)
            for stat_date, total, segments, delivered, failed, unknown in baseline_snapshot:
                self._probe().psql_execute(
                    "INSERT INTO stat_daily(stat_date,dim_type,dim_value,category,"
                    "total,total_segments,delivered,failed,unknown_cnt) VALUES("
                    "CAST(:'stat_date' AS date),'app',"
                    "CAST(:'app_id' AS varchar(128)),'verify',"
                    "CAST(:'total' AS integer),CAST(:'segments' AS integer),"
                    "CAST(:'delivered' AS integer),CAST(:'failed' AS integer),"
                    "CAST(:'unknown' AS integer))",
                    stat_date=stat_date,
                    app_id=app_id_raw,
                    total=str(total),
                    segments=str(segments),
                    delivered=str(delivered),
                    failed=str(failed),
                    unknown=str(unknown),
                )

        self.rollback.defer(restore_baseline)
        self._probe().psql_execute(
            "INSERT INTO stat_daily(stat_date,dim_type,dim_value,category,total,"
            "total_segments,delivered,failed,unknown_cnt) "
            "SELECT current_date-days_ago,'app',CAST(:'app_id' AS varchar(128)),"
            "'verify',1,1,1,0,0 FROM generate_series(1,7) AS series(days_ago) "
            "ON CONFLICT(stat_date,dim_type,dim_value,category) DO UPDATE "
            "SET total=1,total_segments=1,delivered=1,failed=0,unknown_cnt=0",
            app_id=app_id_raw,
        )
        volume_key = f"quota:volume:app:{app_id_raw}:verify:{self._today()}"
        before_volume = self._probe().redis_int(volume_key)
        scheduled_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        response = self._request(
            self.api,
            "POST",
            "/api/v1/messages/send",
            payload={
                "category": "verify",
                "mobiles": [self.phone(26, index) for index in range(500)],
                "content": "验证码135790",
                "scheduled_at": scheduled_at,
                "biz_id": f"u26-{self.run_id}",
            },
            headers=self._api_key("app-iam"),
        )
        data = self._expect("26", response, 200)
        batch_no = data.get("batch_no")
        if (
            data.get("accepted") != 500
            or data.get("status") != "scheduled"
            or not isinstance(batch_no, str)
        ):
            raise UatFailure("UAT-26 anomaly volume batch mismatch")
        self.rollback.defer(
            lambda: self._cleanup_http(
                "26",
                "POST",
                f"/api/v1/messages/batches/{batch_no}/cancel",
                headers=self._api_key("app-iam"),
            )
        )
        if self._probe().redis_int(volume_key) - before_volume != 500:
            raise UatFailure("UAT-26 anomaly volume counter mismatch")
        before_alerts = self._alerts("26", "anomaly")
        before_ids = {item["id"] for item in before_alerts if isinstance(item.get("id"), int)}
        run_count = self._probe().psql_count(
            "SELECT count(*) FROM job_run WHERE job_name='anomaly_scan' AND status='success'"
        )
        self._trigger_job("26", "anomaly_scan")
        wait_until(
            "26",
            lambda: (
                True
                if self._probe().psql_count(
                    "SELECT count(*) FROM job_run "
                    "WHERE job_name='anomaly_scan' AND status='success'"
                )
                > run_count
                else None
            ),
            timeout_s=20,
            interval_s=0.5,
        )

        def anomaly_alert() -> dict[str, Any] | None:
            for alert in self._alerts("26", "anomaly"):
                detail = alert.get("detail")
                if (
                    alert.get("id") not in before_ids
                    and alert.get("level") == "crit"
                    and alert.get("channels") == "log-sink"
                    and isinstance(detail, dict)
                    and detail.get("app_id") == int(app_id_raw)
                    and detail.get("category") == "verify"
                ):
                    return alert
            return None

        alert = wait_until("26", anomaly_alert, timeout_s=20, interval_s=0.5)
        recommendation = alert.get("detail", {}).get("recommendation")
        if (
            "验证码发送量异常" not in str(alert.get("title"))
            or "核查" not in str(recommendation)
            or "停用" not in str(recommendation)
        ):
            raise UatFailure("UAT-26 critical recommendation missing")
        first_alert_count = len(self._alerts("26", "anomaly"))
        run_count += 1
        self._trigger_job("26", "anomaly_scan")
        wait_until(
            "26",
            lambda: (
                True
                if self._probe().psql_count(
                    "SELECT count(*) FROM job_run "
                    "WHERE job_name='anomaly_scan' AND status='success'"
                )
                > run_count
                else None
            ),
            timeout_s=20,
            interval_s=0.5,
        )
        if len(self._alerts("26", "anomaly")) != first_alert_count:
            raise UatFailure("UAT-26 anomaly alert was not deduplicated")

    def case_27(self) -> None:
        job_name = "dispatch_callbacks"
        initial_runs = self._probe().psql_count(
            "SELECT count(*) FROM job_run WHERE job_name='dispatch_callbacks' AND status='success'"
        )
        self._trigger_job("27", job_name)
        wait_until(
            "27",
            lambda: (
                True
                if self._probe().psql_count(
                    "SELECT count(*) FROM job_run "
                    "WHERE job_name='dispatch_callbacks' AND status='success'"
                )
                > initial_runs
                else None
            ),
            timeout_s=20,
            interval_s=0.5,
        )
        previous_alert_ids = {
            item["id"]
            for item in self._alerts("27", "job_stalled")
            if isinstance(item.get("id"), int)
        }
        self._probe().stop_beat(self.rollback)

        def stalled_alert() -> dict[str, Any] | None:
            for alert in self._alerts("27", "job_stalled"):
                detail = alert.get("detail")
                if (
                    alert.get("id") not in previous_alert_ids
                    and isinstance(detail, dict)
                    and detail.get("job_name") == job_name
                ):
                    return alert
            return None

        wait_until("27", stalled_alert, timeout_s=130, interval_s=1)
        jobs_response = self._request(
            self.api,
            "GET",
            "/api/v1/web/admin/jobs",
            headers=self._bearer("admin01"),
        )
        if jobs_response.status != 200 or not isinstance(jobs_response.data, list):
            raise UatFailure("UAT-27 jobs response missing")
        if not any(
            isinstance(item, dict)
            and item.get("job_name") == job_name
            and item.get("stalled") is True
            for item in jobs_response.data
        ):
            raise UatFailure("UAT-27 stalled job was not visible")
        self._probe().start_beat()
        restored_runs = self._probe().psql_count(
            "SELECT count(*) FROM job_run WHERE job_name='dispatch_callbacks' AND status='success'"
        )
        self._trigger_job("27", job_name)

        def healthy_job() -> dict[str, Any] | None:
            if (
                self._probe().psql_count(
                    "SELECT count(*) FROM job_run "
                    "WHERE job_name='dispatch_callbacks' AND status='success'"
                )
                <= restored_runs
            ):
                return None
            response = self._request(
                self.api,
                "GET",
                "/api/v1/web/admin/jobs",
                headers=self._bearer("admin01"),
            )
            if response.status != 200 or not isinstance(response.data, list):
                return None
            for item in response.data:
                if (
                    isinstance(item, dict)
                    and item.get("job_name") == job_name
                    and item.get("last_status") == "success"
                    and item.get("stalled") is False
                ):
                    return item
            return None

        wait_until("27", healthy_job, timeout_s=20, interval_s=0.5)
        audit = self._expect(
            "27",
            self._request(
                self.api,
                "GET",
                "/api/v1/web/admin/audit-logs?"
                + urllib.parse.urlencode(
                    {
                        "actor": "admin01",
                        "action": "job_trigger",
                        "object_type": "job",
                        "page": 1,
                        "page_size": 100,
                    }
                ),
                headers=self._bearer("admin01"),
            ),
            200,
        )
        audit_items = audit.get("items")
        if not isinstance(audit_items, list) or not any(
            isinstance(item, dict) and item.get("object_id") == job_name for item in audit_items
        ):
            raise UatFailure("UAT-27 manual trigger audit missing")

    def run(self, case_ids: Sequence[str]) -> list[str]:
        completed: list[str] = []
        for case_id in case_ids:
            if case_id not in CASE_IDS:
                raise ValueError("unknown UAT case")
            function = getattr(self, f"case_{case_id}", None)
            if not callable(function):
                raise UatFailure(f"UAT-{case_id} is not implemented")
            function()
            completed.append(case_id)
            print(
                json.dumps({"case": case_id, "status": "success"}, ensure_ascii=False),
                flush=True,
            )
        return completed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--mock-base", default="http://localhost:9028")
    parser.add_argument("--keys", type=Path, required=True)
    parser.add_argument(
        "--mock-password-file",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "deploy/secrets/ldap_bind_password",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deploy/docker-compose.yml",
    )
    parser.add_argument("--cases", default=",".join(CASE_IDS))
    args = parser.parse_args(argv)
    selected = tuple(item.strip() for item in args.cases.split(",") if item.strip())
    repository_root = Path(__file__).resolve().parents[1]
    suite = UatSuite(
        HttpClient(args.base),
        HttpClient(args.mock_base),
        load_keys(args.keys),
        mock_password=read_secret_file(
            args.mock_password_file,
            label="mock password",
        ),
        probe=ComposeProbe(
            CommandRunner(),
            compose_file=args.compose_file,
            repository_root=repository_root,
        ),
    )
    failure: Exception | None = None
    completed: list[str] = []
    try:
        completed = suite.run(selected)
    except Exception as error:
        failure = error
    cleanup_errors = suite.rollback.restore()
    if failure is not None:
        if isinstance(failure, (UatFailure, ValueError)):
            raise failure
        raise UatFailure(f"UAT failed: {type(failure).__name__}") from None
    if cleanup_errors:
        raise UatFailure(f"UAT cleanup failed: {','.join(cleanup_errors)}")
    print(json.dumps({"status": "success", "cases": completed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UatFailure, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
