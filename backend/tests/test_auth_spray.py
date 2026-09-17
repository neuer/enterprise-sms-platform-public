from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.auth.admission_policy import AdmissionLimits
from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    SessionStateUnavailable,
)
from app.core.auth.service import AuthService
from app.core.auth.spray import PasswordSprayGuard


@pytest.mark.asyncio
async def test_spray_keys_are_bounded_normalized_and_no_credentials() -> None:
    store = SimpleNamespace(eval=AsyncMock(return_value=[12, 4, 1, 1]))
    loader = AsyncMock(return_value=SimpleNamespace(limits=AdmissionLimits()))
    guard = PasswordSprayGuard(store, loader, key=b"synthetic-only")
    assert await guard.record_failure("  Victim  ", "::ffff:192.0.2.1") == 0.25
    first = store.eval.call_args
    assert await guard.record_failure("victim", "192.0.2.1") == 0.25
    assert first == store.eval.call_args
    assert "victim" not in str(first) and "192.0.2.1" not in str(first)
    store.eval.return_value = [-1, 0, -1, 0]
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("victim", "192.0.2.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, InvalidCredentials(), ProviderCapacityUnavailable()])
async def test_only_invalid_credentials_signal_and_delay_after_work_release(
    error: Exception | None,
) -> None:
    events = []

    async def release(*args: object) -> None:
        events.append("release")

    async def delay(seconds: float) -> None:
        events.append("delay")

    spray = SimpleNamespace(record_failure=AsyncMock(return_value=0.25), delay=delay)
    guard = SimpleNamespace(
        admit=AsyncMock(return_value=object()),
        spray=spray,
        snapshot=AsyncMock(),
        record_failure=AsyncMock(),
        record_provider_success=AsyncMock(),
        admission=SimpleNamespace(release_work=release),
    )
    identity = AuthenticatedIdentity("local", "victim", "victim", "Synthetic", "", ())
    providers = SimpleNamespace(authenticate=AsyncMock(return_value=identity, side_effect=error))
    auth = AuthService(providers, guard)
    if error is not None:
        with pytest.raises(type(error)):
            await auth.authenticate("local", "victim", "synthetic-password", "192.0.2.1")
    else:
        assert (
            await auth.authenticate("local", "victim", "synthetic-password", "192.0.2.1")
        ).login_name == "victim"
    assert events == (
        ["release", "delay"] if isinstance(error, InvalidCredentials) else ["release"]
    )
    assert spray.record_failure.await_count == int(isinstance(error, InvalidCredentials))
