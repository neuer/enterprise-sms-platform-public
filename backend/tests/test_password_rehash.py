"""透明密码摘要维护的认证、容量与参数边界。"""
from __future__ import annotations

from dataclasses import replace

import pytest
from argon2 import PasswordHasher, Type

from app.core.auth.accounts import LocalAccountRecord
from app.core.auth.backends import InvalidCredentials, ProviderUnavailable, SessionStateUnavailable
from app.core.auth.local import LocalPasswordProvider
from app.core.auth.passwords import LocalPasswordHasher
from app.core.bounded_executor import ExecutorBackpressure
from tests.test_local_auth_provider import FakeLocalRepository, RecordingHasher, account


class UpgradeHasher(RecordingHasher):
    def __init__(self):
        super().__init__(matches=True)
        self.hash_calls = 0

    def needs_rehash(self, encoded):
        return encoded == "old"

    def hash(self, password):
        self.hash_calls += 1
        return "upgraded"


class UpgradeRepository(FakeLocalRepository):
    def __init__(self, record, *, conflict=False, error=None):
        super().__init__(record)
        self.calls = []
        self.conflict = conflict
        self.error = error

    async def upgrade_local_password_hash(self, *, expected, password_hash):
        self.calls.append((expected, password_hash))
        if self.error:
            raise self.error
        if self.conflict or self.record != expected:
            return False
        self.record = replace(expected, password_hash=password_hash)
        return True


@pytest.mark.parametrize("pool", ["auth_login_hash", "auth_hash"])
@pytest.mark.asyncio
async def test_upgrade_uses_maintenance_pool_and_preserves_snapshot(monkeypatch, pool):
    record = LocalAccountRecord(account(), "old", credential_version=4)
    repo = UpgradeRepository(record)
    hasher = UpgradeHasher()
    pools = []

    async def bounded(function, *args, pool, **kwargs):
        pools.append(pool)
        return function(*args)

    monkeypatch.setattr("app.core.auth.local.run_bounded", bounded)
    provider = LocalPasswordProvider(repo, hasher)
    assert (await provider.authenticate("admin", "candidate", pool=pool)).account == record.account
    assert pools == [pool, "auth_hash"]
    assert repo.record == replace(record, password_hash="upgraded")
    await provider.authenticate("admin", "candidate", pool=pool)
    assert len(repo.calls) == 1
    assert hasher.hash_calls == 1


@pytest.mark.parametrize("error", [ExecutorBackpressure("full"), TimeoutError()])
@pytest.mark.asyncio
async def test_optional_upgrade_capacity_failure_does_not_write(monkeypatch, error):
    record = LocalAccountRecord(account(), "old")
    repo = UpgradeRepository(record)

    async def bounded(function, *args, pool, **kwargs):
        if function.__name__ == "hash":
            raise error
        return function(*args)

    monkeypatch.setattr("app.core.auth.local.run_bounded", bounded)
    result = await LocalPasswordProvider(repo, UpgradeHasher()).authenticate("admin", "candidate")
    assert result.account == record.account
    assert repo.record == record
    assert repo.calls == []


@pytest.mark.asyncio
async def test_upgrade_conflict_cannot_authenticate_old_snapshot():
    repo = UpgradeRepository(LocalAccountRecord(account(), "old"), conflict=True)
    with pytest.raises(InvalidCredentials):
        await LocalPasswordProvider(repo, UpgradeHasher()).authenticate("admin", "candidate")
    assert repo.record.password_hash == "old"


@pytest.mark.asyncio
async def test_upgrade_database_failure_is_not_optional():
    repo = UpgradeRepository(
        LocalAccountRecord(account(), "old"), error=SessionStateUnavailable("unavailable"),
    )
    with pytest.raises(SessionStateUnavailable):
        await LocalPasswordProvider(repo, UpgradeHasher()).authenticate("admin", "candidate")


@pytest.mark.asyncio
async def test_unexpected_hash_failure_has_controlled_credential_free_error():
    class BrokenHasher(UpgradeHasher):
        def hash(self, password):
            raise ValueError("candidate secret hash details")

    repo = UpgradeRepository(LocalAccountRecord(account(), "old"))
    with pytest.raises(ProviderUnavailable) as caught:
        await LocalPasswordProvider(repo, BrokenHasher()).authenticate("admin", "candidate")
    assert "candidate" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert not repo.calls


@pytest.mark.parametrize("state", ["wrong", "missing", "disabled", "expired"])
@pytest.mark.asyncio
async def test_invalid_credential_never_enters_upgrade(state):
    value = account(account_enabled=state != "disabled", must_change_password=state == "expired")
    repo = UpgradeRepository(None if state == "missing" else LocalAccountRecord(value, "old"))
    hasher = UpgradeHasher()
    hasher.matches = state != "wrong"
    with pytest.raises(InvalidCredentials):
        await LocalPasswordProvider(repo, hasher).authenticate("admin", "candidate")
    assert hasher.hash_calls == 0
    assert not repo.calls


@pytest.mark.parametrize(
    ("options", "upgrade"),
    [
        ({"time_cost": 1, "memory_cost": 8192}, True),
        ({"type": Type.I}, True),
        ({"time_cost": 4}, False),
        ({"memory_cost": 131072}, False),
        ({"memory_cost": 131072, "time_cost": 1}, False),
        ({"parallelism": 8}, False),
        ({"hash_len": 48}, False),
        ({"salt_len": 24}, False),
        ({}, False),
    ],
)
def test_upgrade_policy_never_downgrades_stronger_or_incomparable_parameters(options, upgrade):
    configuration = dict(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16,
                         type=Type.ID)
    configuration.update(options)
    # 参数判断无需重复昂贵计算；用真实编码替换成本字段即可。
    encoded = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=4).hash("synthetic")
    prefix, version, costs, salt, digest = encoded[1:].split("$")
    prefix = "argon2i" if configuration["type"] == Type.I else "argon2id"
    costs = (f"m={configuration['memory_cost']},t={configuration['time_cost']},"
             f"p={configuration['parallelism']}")
    import base64
    salt = base64.b64encode(b"s" * configuration["salt_len"]).decode().rstrip("=")
    digest = base64.b64encode(b"d" * configuration["hash_len"]).decode().rstrip("=")
    candidate = f"${prefix}${version}${costs}${salt}${digest}"
    assert LocalPasswordHasher().needs_rehash(candidate) == upgrade
