"""智慧信息厂商的确定性内存模拟器，仅供开发和自动化测试。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from threading import RLock
from time import monotonic
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

PHONE_PATTERN = re.compile(r"^1\d{10}$")
FAILED_REPORT_PREFIX = "1990000"
NO_REPORT_PREFIX = "1991000"
DEFAULT_BALANCE = 5000


def _envelope(data: Any = None, *, code: int = 0, msg: str | None = None) -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def _vendor_time() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class MockControl(BaseModel):
    """测试控制面允许的确定性注入项。"""

    model_config = ConfigDict(extra="forbid")

    next_send_code: int | None = None
    times: int | None = None
    clear_send_error: bool = False
    latency_ms: int | None = None
    balance: int | None = None
    requeue_reports: bool = False
    enqueue_report: dict[str, Any] | None = None
    enqueue_reply: dict[str, Any] | None = None
    callback_failures: int | None = None
    callback_status: int | None = None
    clear_callbacks: bool = False
    retain_callback_count: int | None = None
    reset: bool = False


class MockState:
    """单进程 mock 状态；锁只保护极短的内存变更。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", RLock()):
            self.next_send_code: int | None = None
            self.next_send_times = 0
            self.latency_ms = 0
            self.balance = DEFAULT_BALANCE
            self.task_sequence = 0
            self.template_sequence = 0
            self.sign_sequence = 0
            self.send_calls: list[dict[str, Any]] = []
            self.pending_reports: list[dict[str, Any]] = []
            self.consumed_reports: list[dict[str, Any]] = []
            self.pending_replies: list[dict[str, Any]] = []
            self.templates: dict[int, str] = {}
            self.signs: dict[int, str] = {}
            self.callback_failures = 0
            self.callback_status = 500
            self.callbacks: list[dict[str, Any]] = []


STATE = MockState()
app = FastAPI(title="智慧信息厂商 Mock", version="1.0.0")


@app.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "alive"}


async def _apply_latency() -> None:
    with STATE.lock:
        latency_ms = STATE.latency_ms
    if latency_ms:
        await asyncio.sleep(latency_ms / 1000)


def _validate_phone_record(record: dict[str, Any], kind: str) -> dict[str, Any]:
    phone = record.get("phone")
    if not isinstance(phone, str) or PHONE_PATTERN.fullmatch(phone) is None:
        raise HTTPException(status_code=422, detail=f"{kind}.phone must match ^1\\d{{10}}$")
    return dict(record)


@app.post("/Sms/Api/Send")
async def send(payload: dict[str, Any]) -> dict[str, Any]:
    """记录下发并为非永不报告号码创建延迟 2 秒的报告。"""

    await _apply_latency()
    with STATE.lock:
        if STATE.next_send_code is not None and STATE.next_send_times > 0:
            code = STATE.next_send_code
            STATE.next_send_times -= 1
            if STATE.next_send_times == 0:
                STATE.next_send_code = None
            return _envelope(code=code, msg=f"mock injected error {code}")

        mobile = payload.get("mobile")
        content = payload.get("content")
        if not isinstance(mobile, str) or not isinstance(content, str):
            return _envelope(code=5000, msg="missing mobile/content")
        phones = mobile.split(",")
        if any(PHONE_PATTERN.fullmatch(phone) is None for phone in phones):
            return _envelope(code=1001, msg="invalid mobile")

        STATE.task_sequence += 1
        task_id = str(STATE.task_sequence)
        custom_id = str(payload.get("customId") or "")
        STATE.send_calls.append(
            {
                "taskId": task_id,
                "customId": custom_id,
                "mobile": mobile,
                "content": content,
            }
        )
        available_at = monotonic() + 2.0
        for phone in phones:
            if phone.startswith(NO_REPORT_PREFIX):
                continue
            failed = phone.startswith(FAILED_REPORT_PREFIX)
            STATE.pending_reports.append(
                {
                    "taskId": task_id,
                    "customId": custom_id,
                    "phone": phone,
                    "reportStatus": 2 if failed else 1,
                    "reportDescription": "MOCK_FAILED" if failed else "DELIVRD",
                    "reportTime": _vendor_time(),
                    "_available_at": available_at,
                }
            )
        return _envelope(task_id)


@app.post("/Sms/Api/GetReport")
async def get_report(_: dict[str, Any]) -> dict[str, Any]:
    """只返回已到生成时间的报告，并在返回时消费。"""

    await _apply_latency()
    now = monotonic()
    with STATE.lock:
        ready = [item for item in STATE.pending_reports if item["_available_at"] <= now]
        STATE.pending_reports = [
            item for item in STATE.pending_reports if item["_available_at"] > now
        ]
        STATE.consumed_reports.extend(ready)
        data = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in ready
        ]
    return _envelope(data)


@app.post("/Sms/Api/GetReply")
async def get_reply(_: dict[str, Any]) -> dict[str, Any]:
    """拉走并消费全部已注入的上行回复。"""

    await _apply_latency()
    with STATE.lock:
        data = STATE.pending_replies
        STATE.pending_replies = []
    return _envelope(data)


@app.post("/Sms/Api/GetBalance")
async def get_balance(_: dict[str, Any]) -> dict[str, Any]:
    await _apply_latency()
    with STATE.lock:
        return _envelope(STATE.balance)


@app.post("/Sms/Api/BindTemplate")
async def bind_template(payload: dict[str, Any]) -> dict[str, Any]:
    await _apply_latency()
    content = payload.get("templateContent")
    if not isinstance(content, str) or not content:
        return _envelope(code=5000, msg="missing templateContent")
    with STATE.lock:
        STATE.template_sequence += 1
        STATE.templates[STATE.template_sequence] = content
        return _envelope(STATE.template_sequence)


@app.post("/Sms/Api/GetTemplateState")
async def get_template_state(payload: dict[str, Any]) -> dict[str, Any]:
    await _apply_latency()
    ids = payload.get("templateIds")
    if not isinstance(ids, list):
        return _envelope(code=5000, msg="missing templateIds")
    with STATE.lock:
        data = [
            {"id": item, "checkType": 1, "checkRemark": None}
            for item in ids
            if item in STATE.templates
        ]
    return _envelope(data)


@app.post("/Sms/Api/BindSign")
async def bind_sign(payload: dict[str, Any]) -> dict[str, Any]:
    await _apply_latency()
    name = payload.get("signName")
    if not isinstance(name, str) or not name:
        return _envelope(code=5000, msg="missing signName")
    with STATE.lock:
        STATE.sign_sequence += 1
        STATE.signs[STATE.sign_sequence] = name
        return _envelope(STATE.sign_sequence)


@app.post("/Sms/Api/GetSignState")
async def get_sign_state(payload: dict[str, Any]) -> dict[str, Any]:
    await _apply_latency()
    ids = payload.get("signIds")
    if not isinstance(ids, list):
        return _envelope(code=5000, msg="missing signIds")
    with STATE.lock:
        data = [
            {"id": item, "checkType": 1, "checkRemark": None}
            for item in ids
            if item in STATE.signs
        ]
    return _envelope(data)


@app.post("/_mock/state")
async def configure_mock(control: MockControl) -> dict[str, Any]:
    """应用测试注入；手机号仅可进入进程内队列，不在响应中回显。"""

    if control.reset:
        STATE.reset()
    with STATE.lock:
        if control.clear_send_error and control.next_send_code is not None:
            raise HTTPException(
                status_code=422,
                detail="clear_send_error conflicts with next_send_code",
            )
        if control.clear_send_error:
            STATE.next_send_code = None
            STATE.next_send_times = 0
        elif control.next_send_code is not None:
            STATE.next_send_code = control.next_send_code
            STATE.next_send_times = control.times if control.times is not None else 1
        elif control.times is not None:
            raise HTTPException(status_code=422, detail="times requires next_send_code")
        if control.latency_ms is not None:
            if control.latency_ms < 0:
                raise HTTPException(status_code=422, detail="latency_ms must be non-negative")
            STATE.latency_ms = control.latency_ms
        if control.balance is not None:
            STATE.balance = control.balance
        if control.requeue_reports:
            STATE.pending_reports.extend(dict(item) for item in STATE.consumed_reports)
        if control.enqueue_report is not None:
            report = _validate_phone_record(control.enqueue_report, "enqueue_report")
            report.setdefault("reportDescription", "INJECTED")
            report.setdefault("reportTime", _vendor_time())
            report["_available_at"] = 0.0
            STATE.pending_reports.append(report)
        if control.enqueue_reply is not None:
            reply = _validate_phone_record(control.enqueue_reply, "enqueue_reply")
            reply.setdefault("extCode", "")
            reply.setdefault("replyTime", _vendor_time())
            STATE.pending_replies.append(reply)
        if control.callback_failures is not None:
            if control.callback_failures < 0:
                raise HTTPException(
                    status_code=422,
                    detail="callback_failures must be non-negative",
                )
            STATE.callback_failures = control.callback_failures
        if control.callback_status is not None:
            if not 400 <= control.callback_status <= 599:
                raise HTTPException(status_code=422, detail="callback_status must be 4xx/5xx")
            STATE.callback_status = control.callback_status
        if control.clear_callbacks:
            STATE.callbacks.clear()
        if control.retain_callback_count is not None:
            if not 0 <= control.retain_callback_count <= len(STATE.callbacks):
                raise HTTPException(
                    status_code=422,
                    detail="retain_callback_count exceeds callback history",
                )
            del STATE.callbacks[control.retain_callback_count :]
    return _public_state()


def _public_state() -> dict[str, Any]:
    with STATE.lock:
        send_calls = [
            item
            | {
                "mobile": ",".join(_mask_phone(phone) for phone in item["mobile"].split(","))
            }
            for item in STATE.send_calls
        ]
        return {
            "next_send_code": STATE.next_send_code,
            "next_send_times": STATE.next_send_times,
            "latency_ms": STATE.latency_ms,
            "balance": STATE.balance,
            "pending_reports": len(STATE.pending_reports),
            "pending_replies": len(STATE.pending_replies),
            "send_calls": send_calls,
            "template_contents": list(STATE.templates.values()),
            "callback_failures": STATE.callback_failures,
            "callback_status": STATE.callback_status,
            "callback_count": len(STATE.callbacks),
        }


@app.get("/_mock/state")
async def read_mock_state() -> dict[str, Any]:
    return _public_state()


@app.post("/_mock/callback")
async def callback_sink(request: Request) -> Response:
    """保存回调原始字节和签名头，并按配置确定性返回失败。"""

    raw_body = await request.body()
    with STATE.lock:
        STATE.callbacks.append(
            {
                "raw_body": raw_body.decode("utf-8", errors="replace"),
                "signature": request.headers.get("X-Sms-Signature"),
                "timestamp": request.headers.get("X-Sms-Timestamp"),
            }
        )
        if STATE.callback_failures > 0:
            STATE.callback_failures -= 1
            return Response(status_code=STATE.callback_status)
    return Response(status_code=200)


@app.get("/_mock/callbacks")
async def read_callbacks() -> list[dict[str, Any]]:
    with STATE.lock:
        return [dict(item) for item in STATE.callbacks]


@app.delete("/_mock/callbacks")
async def clear_callbacks() -> dict[str, str]:
    with STATE.lock:
        STATE.callbacks.clear()
    return {"status": "ok"}
