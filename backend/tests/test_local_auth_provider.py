from __future__ import annotations

import pytest

from app.core.auth.accounts import LocalAccountRecord, PlatformAccount
from app.core.auth.backends import InvalidCredentials, ProviderCapacityUnavailable
from app.core.auth.local import LocalPasswordProvider
from app.core.bounded_executor import ExecutorBackpressure


def account(
    *,
    account_enabled: bool = True,
    identity_enabled: bool = True,
    must_change_password: bool = False,
) -> PlatformAccount:
    return PlatformAccount(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="admin",
        normalized_login_name="admin",
        display_name="管理员",
        dept="平台部",
        role="admin",
        security_version=3,
        account_enabled=account_enabled,
        identity_enabled=identity_enabled,
        must_change_password=must_change_password,
    )


class FakeLocalRepository:
    def __init__(self, record: LocalAccountRecord | None) -> None:
        self.record = record
        self.login_names: list[str] = []

    async def find_local_account(self, normalized_login_name: str) -> LocalAccountRecord | None:
        self.login_names.append(normalized_login_name)
        return self.record


class RecordingHasher:
    def __init__(self, *, matches: bool = False) -> None:
        self.matches = matches
        self.candidates: list[tuple[str | None, str]] = []

    def verify_or_dummy(self, encoded: str | None, candidate: str) -> bool:
        self.candidates.append((encoded, candidate))
        return self.matches


@pytest.mark.asyncio
async def test_local_provider_uses_dummy_hash_for_missing_identity() -> None:
    hasher = RecordingHasher()
    repository = FakeLocalRepository(None)
    provider = LocalPasswordProvider(repository, hasher)

    with pytest.raises(InvalidCredentials):
        await provider.authenticate(" Missing ", "Wrong@Password123")

    assert repository.login_names == ["missing"]
    assert hasher.candidates == [(None, "Wrong@Password123")]


@pytest.mark.asyncio
async def test_local_provider_returns_account_and_must_change_password_state() -> None:
    value = account(must_change_password=True)
    record = LocalAccountRecord(value, "$argon2id$v=19$valid", temporary_password_valid=True)
    hasher = RecordingHasher(matches=True)
    provider = LocalPasswordProvider(FakeLocalRepository(record), hasher)

    authenticated = await provider.authenticate("  AdMiN ", "Valid@Password123")

    assert authenticated.account == value
    assert authenticated.account is not None
    assert authenticated.account.must_change_password
    assert authenticated.external_subject == "local:admin"
    assert hasher.candidates == [(record.password_hash, "Valid@Password123")]


@pytest.mark.parametrize(
    "value",
    (
        account(account_enabled=False),
        account(identity_enabled=False),
    ),
)
@pytest.mark.asyncio
async def test_local_provider_rejects_disabled_account_or_identity(
    value: PlatformAccount,
) -> None:
    record = LocalAccountRecord(value, "$argon2id$v=19$valid")
    hasher = RecordingHasher(matches=True)
    provider = LocalPasswordProvider(FakeLocalRepository(record), hasher)

    with pytest.raises(InvalidCredentials):
        await provider.authenticate("admin", "Valid@Password123")

    assert hasher.candidates == [(record.password_hash, "Valid@Password123")]


@pytest.mark.asyncio
async def test_local_provider_rejects_wrong_password_after_real_hash_work() -> None:
    record = LocalAccountRecord(account(), "$argon2id$v=19$valid")
    hasher = RecordingHasher(matches=False)
    provider = LocalPasswordProvider(FakeLocalRepository(record), hasher)

    with pytest.raises(InvalidCredentials):
        await provider.authenticate("admin", "Wrong@Password123")

    assert hasher.candidates == [(record.password_hash, "Wrong@Password123")]


@pytest.mark.asyncio
async def test_local_provider_maps_executor_backpressure_to_capacity_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> bool:
        raise ExecutorBackpressure("full")

    monkeypatch.setattr("app.core.auth.local.run_bounded", boom)
    provider = LocalPasswordProvider(
        FakeLocalRepository(LocalAccountRecord(account(), "$argon2id$v=19$valid")),
        RecordingHasher(matches=True),
    )

    with pytest.raises(ProviderCapacityUnavailable):
        await provider.authenticate("admin", "Valid@Password123")


@pytest.mark.asyncio
async def test_local_provider_routes_login_and_reauthentication_to_separate_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    async def record_pool(
        function: object,
        *_args: object,
        timeout_s: float,
        pool: str,
        **_kwargs: object,
    ) -> bool:
        del function, timeout_s
        observed.append(pool)
        return True

    monkeypatch.setattr("app.core.auth.local.run_bounded", record_pool)
    record = LocalAccountRecord(account(), "$argon2id$v=19$valid")
    provider = LocalPasswordProvider(FakeLocalRepository(record), RecordingHasher())

    await provider.authenticate("admin", "Valid@Password123")
    await provider.authenticate("admin", "Valid@Password123", pool="auth_hash")

    assert observed == ["auth_login_hash", "auth_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_temporary_password_rechecks_database_validity_after_hash(valid: bool) -> None:
    value = account(must_change_password=True)
    record = LocalAccountRecord(value, "encoded", temporary_password_valid=valid)
    hasher = RecordingHasher(matches=True)
    repo = FakeLocalRepository(record)
    provider = LocalPasswordProvider(repo, hasher)
    if valid:
        assert (await provider.authenticate("admin", "candidate")).account == value
    else:
        with pytest.raises(InvalidCredentials):
            await provider.authenticate("admin", "candidate")
    assert len(hasher.candidates) == 1
    assert len(repo.login_names) == 2


@pytest.mark.asyncio
async def test_temporary_password_reset_during_hash_rejects_old_credential() -> None:
    from dataclasses import replace

    record = LocalAccountRecord(
        account(must_change_password=True), "old", temporary_password_valid=True
    )
    repo = FakeLocalRepository(record)

    class ResetHasher(RecordingHasher):
        def verify_or_dummy(self, encoded, candidate):
            repo.record = replace(record, password_hash="new", credential_version=2)
            return True

    with pytest.raises(InvalidCredentials):
        await LocalPasswordProvider(repo, ResetHasher()).authenticate("admin", "candidate")
