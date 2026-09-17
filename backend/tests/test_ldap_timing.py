"""真实 Provider 边界的合成连接回归；不连接目录、不提供真实凭据。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ldap3.core.exceptions import LDAPBindError, LDAPInvalidCredentialsResult

from app.core.auth import ldap_real
from app.core.auth.backends import InvalidCredentials, ProviderUnavailable
from app.core.auth.ldap_timing import LdapTimingProfile, load_ldap_timing_profile
from tests.test_auth import _ldap_config


@pytest.mark.parametrize("enabled,profile_present", [(False, False), (True, False), (True, True)])
async def test_readiness_requires_mounted_profile_only_for_enabled_ad(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool, profile_present: bool,
) -> None:
    from app.core import health

    config = configuration()
    path = tmp_path / "ldap-timing.json"
    if profile_present:
        assert config.timing_profile is not None
        path.write_text(config.timing_profile.model_dump_json(), encoding="utf-8")

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            pass

        async def execute(self, _query: Any) -> Any:
            configs = [{"server": config.server, "bind_dn": config.bind_dn}] if enabled else []
            return SimpleNamespace(scalars=lambda: configs)

    monkeypatch.setattr(
        health, "database_engine", lambda *a, **kw: SimpleNamespace(connect=Connection),
    )
    check = health.LdapTimingReadinessCheck(
        SimpleNamespace(
            auth_mock=False, ldap_timing_profile_file=path,
            database_url_for=lambda _: "postgresql+asyncpg://synthetic",
        )
    )
    if enabled and not profile_present:
        with pytest.raises(ProviderUnavailable):
            await check()
    else:
        await check()


def configuration() -> ldap_real.LdapConfig:
    return _ldap_config(
        provider_code="ad",
        server="ldaps://dc.example:636",
        base_dn="DC=example",
        bind_dn="CN=svc,DC=example",
        bind_password="synthetic-service",
        user_search_filter="(uid={username})",
        username_attribute="uid",
        display_name_attribute="displayName",
        dept_attribute="department",
        subject_attribute="entryUUID",
        group_attribute="memberOf",
        ca_certs_file="/ca.pem",
        connect_timeout_s=3,
        receive_timeout_s=4,
    )


def connections(
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
    result_code: int = 0,
    second_error: Exception | None = None,
    second_success: bool = False,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Connection:
        def __init__(self, _: Any, **kwargs: Any) -> None:
            self.index = len([item for item in calls if "user" in item])
            self.user = kwargs["user"]
            calls.append(kwargs)
            self.socket = SimpleNamespace()
            self.result = {"result": result_code}
            attribute = SimpleNamespace(value="stable-subject", values=[])
            self.entries = [
                SimpleNamespace(
                    entry_dn="CN=candidate,DC=example",
                    entryUUID=attribute,
                    uid=attribute,
                )
            ] * count

        def open(self) -> None:
            calls.append({"open": self.index})

        def bind(self, **kwargs: Any) -> bool:
            calls.append({"bind": self.index, **kwargs})
            if self.index:
                if second_error:
                    raise second_error
                if not second_success:
                    raise LDAPInvalidCredentialsResult(result=49)
            return True

        def search(self, **kwargs: Any) -> bool:
            calls.append({"search": kwargs})
            return count > 0

        def unbind(self) -> None:
            calls.append({"closed": self.index})

    monkeypatch.setattr(ldap_real, "Tls", lambda **kw: kw)
    monkeypatch.setattr(ldap_real, "Server", lambda *a, **kw: kw)
    monkeypatch.setattr(ldap_real, "Connection", Connection)
    return calls


@pytest.mark.parametrize("count,result_code", [(0, 0), (2, 0), (2, 4), (1, 0)])
async def test_missing_duplicate_and_wrong_password_execute_two_bounded_binds(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    result_code: int,
) -> None:
    calls = connections(monkeypatch, count=count, result_code=result_code)
    config = configuration()
    with pytest.raises(InvalidCredentials, match="用户名或密码错误"):
        await ldap_real.LdapPasswordProvider(config).authenticate(
            "alias*)(uid=*)", "candidate-pass"
        )
    binds = [item for item in calls if "user" in item]
    assert len(binds) == 2
    assert binds[0]["user"] == config.bind_dn
    assert binds[1]["user"] == (
        "CN=candidate,DC=example" if count == 1 else config.timing_profile.sink_dn
    )
    assert binds[1]["password"] != config.bind_password
    assert (binds[1]["password"] == "candidate-pass") is (count == 1)
    assert all(item["auto_referrals"] is False for item in binds)
    assert all(0 < item["receive_timeout"] <= 4 for item in binds)
    assert sorted(item["closed"] for item in calls if "closed" in item) == [0, 1]


@pytest.mark.parametrize("failure", [OSError("private-DN"), TimeoutError(), LDAPBindError("bad")])
async def test_sink_failure_is_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    calls = connections(monkeypatch, count=0, second_error=failure)
    with pytest.raises(ProviderUnavailable) as error:
        await ldap_real.LdapPasswordProvider(configuration()).authenticate("unknown", "secret")
    assert "private-DN" not in str(error.value)
    assert sorted(item["closed"] for item in calls if "closed" in item) == [0, 1]


async def test_unexpected_sink_success_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = connections(monkeypatch, count=0, second_success=True)
    with pytest.raises(ProviderUnavailable):
        await ldap_real.LdapPasswordProvider(configuration()).authenticate("unknown", "secret")
    assert len([item for item in calls if "closed" in item]) == 2


@pytest.mark.parametrize("count,code", [(0, 3), (1, 3), (1, 4), (0, 10), (1, 52)])
async def test_partial_search_cannot_authenticate_or_become_credentials_error(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    code: int,
) -> None:
    calls = connections(monkeypatch, count=count, result_code=code, second_success=True)
    with pytest.raises(ProviderUnavailable):
        await ldap_real.LdapPasswordProvider(configuration()).authenticate("candidate", "secret")
    assert len([item for item in calls if "user" in item]) == 1


async def test_legitimate_user_still_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    connections(monkeypatch, count=1, second_success=True)
    identity = await ldap_real.LdapPasswordProvider(configuration()).authenticate("alias", "secret")
    assert identity.external_subject == "stable-subject"
    assert identity.login_name == "stable-subject"


@pytest.mark.parametrize("present", [False, True])
async def test_missing_or_expired_profile_rejects_all_names_before_io(
    monkeypatch: pytest.MonkeyPatch,
    present: bool,
) -> None:
    calls = connections(monkeypatch, count=1, second_success=True)
    config = configuration()
    profile = config.timing_profile
    if present:
        profile = profile.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
    else:
        profile = None
    config = replace(config, timing_profile=profile)
    for name in ("present", "missing"):
        with pytest.raises(ProviderUnavailable):
            await ldap_real.LdapPasswordProvider(config).authenticate(name, "secret")
    assert calls == []


def test_profile_file_boundaries(tmp_path: Path) -> None:
    profile = configuration().timing_profile
    assert profile is not None
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    loaded = load_ldap_timing_profile(path)
    loaded.require_current("ldaps://DC.EXAMPLE", "cn=SVC,dc=EXAMPLE")
    with pytest.raises(ProviderUnavailable):
        loaded.require_current("ldaps://different.example", "CN=svc,DC=example")
    with pytest.raises(ValueError):
        LdapTimingProfile.model_validate(
            {**profile.model_dump(), "sink_dn": profile.service_bind_dn}
        )
    path.write_text("x" * 8193)
    with pytest.raises(ProviderUnavailable):
        load_ldap_timing_profile(path)


async def test_sink_failure_does_not_record_candidate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.auth.service import AuthService, LoginGuard
    from tests.test_auth import FakeKeyValue

    connections(monkeypatch, count=0, second_error=OSError())
    provider = ldap_real.LdapPasswordProvider(configuration())

    class Registry:
        async def authenticate(self, _code: str, name: str, password: str, **_kw: Any) -> Any:
            return await provider.authenticate(name, password)

    store = FakeKeyValue()
    service = AuthService(Registry(), LoginGuard(store))
    with pytest.raises(ProviderUnavailable):
        await service.authenticate("ad", "candidate", "secret", "192.0.2.1")
    assert not any(key.startswith("auth:fail:") for key in store.values)
