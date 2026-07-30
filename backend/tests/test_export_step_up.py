from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from app.core.auth.jwt import JwtClaims
from app.services.export_step_up import (
    EXPORT_STEP_UP_TTL_SECONDS,
    ExportStepUpExpired,
    ExportStepUpService,
)

PUBLIC_ID = UUID("c0a80101-0000-4000-8000-000000000134")
OTHER_PUBLIC_ID = UUID("c0a80101-0000-4000-8000-000000000135")


def claims(*, account_id: int = 11, jti: str = "session-jti") -> JwtClaims:
    return JwtClaims(
        account_id,
        101,
        "local",
        "approver-a",
        "审批员A",
        "平台部",
        "approver",
        1,
        jti,
    )


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[tuple[JwtClaims, str, str]] = []

    async def reauthenticate_current(
        self,
        current: JwtClaims,
        password: str,
        ip: str,
    ) -> None:
        self.calls.append((current, password, ip))


class AtomicStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.expiries[key] = ex

    async def eval(self, _script: str, _numkeys: int, *args: Any) -> int:
        key, expected = str(args[0]), str(args[1])
        async with self.lock:
            stored = self.values.pop(key, None)
            if stored is None:
                return 0
            return 1 if stored == expected else -1


@pytest.mark.asyncio
async def test_issue_binds_stable_subject_session_ip_and_exact_task() -> None:
    auth = FakeAuth()
    store = AtomicStore()
    service = ExportStepUpService(auth, store)
    current = claims()

    token = await service.issue(
        claims=current,
        password="current-password",
        ip="10.0.0.8",
        public_id=PUBLIC_ID,
    )

    assert auth.calls == [(current, "current-password", "10.0.0.8")]
    assert token not in store.values
    assert list(store.expiries.values()) == [EXPORT_STEP_UP_TTL_SECONDS]
    persisted = next(iter(store.values.values()))
    assert '"account_id":11' in persisted
    assert '"identity_id":101' in persisted
    assert '"jti":"session-jti"' in persisted
    assert '"ip":"10.0.0.8"' in persisted
    assert f'"public_id":"{PUBLIC_ID}"' in persisted
    assert "current-password" not in persisted


@pytest.mark.asyncio
async def test_token_is_single_use_and_context_mismatch_burns_it() -> None:
    service = ExportStepUpService(FakeAuth(), AtomicStore())
    current = claims()
    token = await service.issue(
        claims=current,
        password="current-password",
        ip="10.0.0.8",
        public_id=PUBLIC_ID,
    )

    with pytest.raises(ExportStepUpExpired):
        await service.consume(
            token,
            claims=current,
            ip="10.0.0.8",
            public_id=OTHER_PUBLIC_ID,
        )
    with pytest.raises(ExportStepUpExpired):
        await service.consume(
            token,
            claims=current,
            ip="10.0.0.8",
            public_id=PUBLIC_ID,
        )


@pytest.mark.asyncio
async def test_concurrent_consumers_have_exactly_one_winner() -> None:
    service = ExportStepUpService(FakeAuth(), AtomicStore())
    current = claims()
    token = await service.issue(
        claims=current,
        password="current-password",
        ip="10.0.0.8",
        public_id=PUBLIC_ID,
    )

    results = await asyncio.gather(
        *(
            service.consume(
                token,
                claims=current,
                ip="10.0.0.8",
                public_id=PUBLIC_ID,
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )

    assert sum(value is None for value in results) == 1
    assert sum(isinstance(value, ExportStepUpExpired) for value in results) == 1


@pytest.mark.asyncio
async def test_legacy_or_viewer_claims_fail_closed_before_reauthentication() -> None:
    auth = FakeAuth()
    service = ExportStepUpService(auth, AtomicStore())

    with pytest.raises(ExportStepUpExpired):
        await service.issue(
            claims=claims(account_id=0),
            password="current-password",
            ip="10.0.0.8",
            public_id=PUBLIC_ID,
        )
    viewer = JwtClaims(
        12,
        102,
        "local",
        "viewer-a",
        "查看员A",
        "平台部",
        "viewer",
        1,
        "viewer-jti",
    )
    with pytest.raises(ExportStepUpExpired):
        await service.issue(
            claims=viewer,
            password="current-password",
            ip="10.0.0.8",
            public_id=PUBLIC_ID,
        )
    assert auth.calls == []
