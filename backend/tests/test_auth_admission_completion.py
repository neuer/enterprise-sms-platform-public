"""完成阶段使用真实 AuthFacade；覆盖首次改密、身份冲突、签发失败和两种会话。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.core.auth.accounts import AccountSourceConflict
from app.core.auth.admission import AdmissionBusy, AdmissionReservation
from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.runtime import LoginSuccess, PasswordChangeRequired
from app.core.errors import ApiError
from tests.test_auth_runtime import IP, TAB_ID, facade


def prepared(*, change: bool = False):
    service, users, tokens, _hasher = facade(must_change_password=change)
    reservation = AdmissionReservation("request", "source", "shared", 1, "digest", "token")
    service.auth.identity = replace(service.auth.identity, admission=reservation)
    service.auth.record_completed_success = AsyncMock()
    return service, users, tokens


@pytest.mark.parametrize("mode", ["refresh", "access_only"])
async def test_full_session_settles_once_after_token_issue(mode: str) -> None:
    service, _users, tokens = prepared()
    original = tokens.issue_access_only if mode == "access_only" else tokens.issue_pair
    issued = AsyncMock(wraps=original)
    setattr(tokens, "issue_access_only" if mode == "access_only" else "issue_pair", issued)
    result = await service.login(
        "local", "alias", "password", IP, TAB_ID if mode == "refresh" else None, session_mode=mode
    )
    assert isinstance(result, LoginSuccess)
    issued.assert_awaited_once()
    service.auth.record_completed_success.assert_awaited_once_with(service.auth.identity, IP)


async def test_password_change_challenge_does_not_refund() -> None:
    service, _users, _tokens = prepared(change=True)
    assert isinstance(
        await service.login("local", "admin", "password", IP, TAB_ID), PasswordChangeRequired
    )
    service.auth.record_completed_success.assert_not_awaited()


@pytest.mark.parametrize("failure", ["identity", "token", "old_family", "mode"])
async def test_partial_success_and_rejected_login_never_refund(failure: str) -> None:
    service, users, tokens = prepared()
    if failure == "identity":
        users.resolve_identity = AsyncMock(side_effect=AccountSourceConflict("conflict"))
    elif failure == "token":
        tokens.issue_pair = AsyncMock(side_effect=SessionStateUnavailable("unavailable"))
    elif failure == "old_family":
        service._revoke_presented_refresh_family = AsyncMock(
            side_effect=ApiError(503, "UNAVAILABLE", "")
        )
    with pytest.raises(ApiError):
        await service.login("local", "admin", "password", IP, None if failure == "mode" else TAB_ID)
    service.auth.record_completed_success.assert_not_awaited()


async def test_busy_response_only_for_preauth_admission() -> None:
    service, _users, _tokens = prepared()
    service.auth.identity = AdmissionBusy(1)
    with pytest.raises(ApiError) as error:
        await service.login("local", "admin", "password", IP, TAB_ID)
    assert error.value.status_code == 429
    assert error.value.detail == {"auth_admission_retry": True, "retry_after_seconds": 1}
    assert error.value.headers == {"Retry-After": "1"}


async def test_reauth_identity_mismatch_never_refunds() -> None:
    service, users, _tokens = prepared()
    claims = service._claims(replace(users.value, security_version=1))
    with pytest.raises(ApiError):
        await service.reauthenticate_current(claims, "password", IP)
    service.auth.record_completed_success.assert_not_awaited()
