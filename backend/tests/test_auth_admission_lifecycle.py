"""API 正常停机必须等待真实同步工作完成后释放其准入名额。"""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.auth.admission import ADMIT_LUA, RELEASE_LUA, LoginAdmission
from app.core.auth.admission_policy import AdmissionLimits, AdmissionPolicy
from app.core.bounded_executor import bounded_work_scope, run_bounded
from app.settings import Settings
from tests.test_auth_admission_policy import approvals


async def test_lifespan_drains_timed_out_thread_before_closing_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    events: list[str] = []
    thread_finished = Event()

    class Background:
        def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    async def noop() -> None:
        pass

    async def close_resources() -> None:
        events.append("redis_closed")

    monkeypatch.setattr(main, "configure_runtime_resources", lambda *a, **kw: None)
    monkeypatch.setattr(main, "register_task_modules", lambda: None)
    monkeypatch.setattr(main, "create_default_heartbeat_service", Background)
    monkeypatch.setattr(main, "create_runtime_monitor", Background)
    monkeypatch.setattr(main, "get_admission_policy_runtime", lambda _: Background())
    monkeypatch.setattr(main, "create_auth_transition_reconciler", lambda _: Background())
    monkeypatch.setattr(
        main, "get_auth_session_policy_runtime",
        lambda _: SimpleNamespace(reconciler=Background()),
    )
    monkeypatch.setattr(main, "close_runtime_resources", close_resources)

    class Store:
        async def eval(self, script: str, *_args: Any) -> Any:
            if script == ADMIT_LUA:
                return [0, 0]
            assert script == RELEASE_LUA
            assert thread_finished.is_set()
            assert "redis_closed" not in events
            events.append("released")
            return 1

    async def policy() -> AdmissionPolicy:
        return AdmissionPolicy(1, AdmissionLimits(), approvals())

    admission = LoginAdmission(Store(), policy, key=b"synthetic-admission-key-32-bytes-long")
    application = SimpleNamespace(
        state=SimpleNamespace(settings=Settings(), startup_config_gate=SimpleNamespace(ensure=noop))
    )

    def slow_bind() -> None:
        Event().wait(0.1)
        thread_finished.set()

    async with main.create_lifespan(Settings())(application):
        reservation = await admission.admit("ad", "192.0.2.9")
        with bounded_work_scope() as work, pytest.raises(TimeoutError):
            await run_bounded(slow_bind, timeout_s=0.01, pool="ldap")
        await admission.release_work(reservation, work)
        assert admission._finishing and events == []
    assert events == ["released", "redis_closed"]
    assert not admission._finishing


async def test_cancellation_after_admission_eval_still_releases_owned_slot() -> None:
    from app.core.auth.admission import drain_login_admissions

    accepted, response = asyncio.Event(), asyncio.Event()
    released = 0

    class Store:
        async def eval(self, script: str, *_args: Any) -> Any:
            nonlocal released
            if script == ADMIT_LUA:
                accepted.set()
                await response.wait()
                return [0, 0]
            assert script == RELEASE_LUA
            released += 1
            return 1

    async def policy() -> AdmissionPolicy:
        return AdmissionPolicy(1, AdmissionLimits(), approvals())

    admission = LoginAdmission(Store(), policy, key=b"synthetic-admission-key-32-bytes-long")
    request = asyncio.create_task(admission.admit("local", "192.0.2.9"))
    await accepted.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    response.set()
    await drain_login_admissions()
    assert released == 1 and not admission._finishing
