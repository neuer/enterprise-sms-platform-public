from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_reconcile_task_injects_reset_finalizer_and_sums_all_contributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import reconcile as task_module

    captured: dict[str, object] = {}
    recipient_repository = object()
    operation_repository = object()

    class PolicyLoader:
        def __init__(self, settings: object) -> None:
            assert settings == "settings"

        async def load(self) -> object:
            return "policy"

    class Uncertain:
        @classmethod
        def from_policy(
            cls,
            repository: object,
            crypto: object,
            policy: object,
        ) -> Uncertain:
            assert policy == "policy"
            return cls()

        async def run_once(self) -> int:
            return 1

    class Recovery:
        def __init__(self, repository: object, publisher: object) -> None:
            pass

        async def run_once(self) -> int:
            return 2

    class Operations:
        def __init__(
            self,
            repository: object,
            client: object,
            *,
            finalizers: dict[str, object],
        ) -> None:
            captured.update(finalizers)

        async def reconcile_once(self) -> int:
            return 3

    class UatRecovery:
        def __init__(self, repository: object) -> None:
            assert repository is operation_repository

        async def reconcile_once(self) -> int:
            return 4

    monkeypatch.setattr(task_module, "get_settings", lambda: "settings")
    monkeypatch.setattr(
        task_module.CryptoService,
        "from_settings",
        lambda settings: "crypto",
    )
    monkeypatch.setattr(task_module, "SqlRuntimePolicyLoader", PolicyLoader)
    monkeypatch.setattr(task_module, "UncertainReconciler", Uncertain)
    monkeypatch.setattr(task_module, "RecoveryReconciler", Recovery)
    monkeypatch.setattr(task_module, "VendorTestOperationService", Operations)
    monkeypatch.setattr(task_module, "VendorTestUatReconciler", UatRecovery, raising=False)
    monkeypatch.setattr(
        task_module,
        "SqlVendorTestOperationRepository",
        lambda settings: operation_repository,
    )
    monkeypatch.setattr(
        task_module,
        "SqlVendorTestRecipientRepository",
        lambda settings: recipient_repository,
        raising=False,
    )

    async def replay_none(_settings: object) -> int:
        return 0

    monkeypatch.setattr(task_module, "_replay_stale_raw", replay_none)

    assert await task_module._reconcile() == 10
    finalizer = captured["reset_configuration"]
    assert finalizer.repository is recipient_repository


@pytest.mark.asyncio
async def test_reconcile_task_propagates_unexpected_operation_finalizer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import reconcile as task_module

    class PolicyLoader:
        def __init__(self, settings: object) -> None:
            pass

        async def load(self) -> object:
            return object()

    class Uncertain:
        @classmethod
        def from_policy(
            cls,
            repository: object,
            crypto: object,
            policy: object,
        ) -> Uncertain:
            return cls()

        async def run_once(self) -> int:
            return 0

    class Recovery:
        def __init__(self, repository: object, publisher: object) -> None:
            pass

        async def run_once(self) -> int:
            return 0

    class Operations:
        def __init__(
            self,
            repository: object,
            client: object,
            *,
            finalizers: dict[str, object],
        ) -> None:
            pass

        async def reconcile_once(self) -> int:
            raise RuntimeError("private-finalizer-detail")

    class UatRecovery:
        def __init__(self, repository: object) -> None:
            pass

        async def reconcile_once(self) -> int:
            raise AssertionError("control failure must stop before UAT recovery")

    monkeypatch.setattr(task_module, "get_settings", lambda: "settings")
    monkeypatch.setattr(
        task_module.CryptoService,
        "from_settings",
        lambda settings: "crypto",
    )
    monkeypatch.setattr(task_module, "SqlRuntimePolicyLoader", PolicyLoader)
    monkeypatch.setattr(task_module, "UncertainReconciler", Uncertain)
    monkeypatch.setattr(task_module, "RecoveryReconciler", Recovery)
    monkeypatch.setattr(task_module, "VendorTestOperationService", Operations)
    monkeypatch.setattr(task_module, "VendorTestUatReconciler", UatRecovery, raising=False)

    with pytest.raises(RuntimeError, match="private-finalizer-detail"):
        await task_module._reconcile()


@pytest.mark.asyncio
async def test_replay_stale_raw_skips_synthetic_settings_without_control_redis() -> None:
    from types import SimpleNamespace

    from app.tasks import reconcile as task_module

    assert await task_module._replay_stale_raw(SimpleNamespace(database_url="unused")) == 0


@pytest.mark.asyncio
async def test_replay_stale_raw_isolates_poison_and_alerts_once_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.services.raw_replay import RawIntegrityConflict
    from app.tasks import reconcile as task_module

    alerts: list[dict[str, object]] = []

    class Ops:
        def __init__(self, settings: object, redis: object) -> None:
            pass

        async def list_stale_unprocessed_raw_ids(self) -> list[int]:
            return [1, 2, 3]

        async def raw_replay_exhausted(self, raw_id: int) -> bool:
            return raw_id == 1

    class Replay:
        def __init__(self, *args: object) -> None:
            pass

        async def replay(self, raw_id: int, *, actor: str, ip: str) -> int:
            if raw_id == 1:
                raise RawIntegrityConflict("raw vendor envelope is invalid")
            if raw_id == 2:
                raise RuntimeError("process_existing write failed")
            return 5

    class Alerts:
        def __init__(self, settings: object) -> None:
            pass

        async def emit(self, **kwargs: object) -> None:
            alerts.append(kwargs)

    monkeypatch.setattr(task_module, "redis_client", lambda url: object())
    monkeypatch.setattr(task_module, "SqlOpsRepository", Ops)
    monkeypatch.setattr(
        task_module.CryptoService,
        "from_settings",
        lambda settings: object(),
    )
    monkeypatch.setattr(task_module, "SqlAlertService", Alerts)
    monkeypatch.setattr(task_module, "RawReplayService", Replay)
    monkeypatch.setattr(task_module, "ReportIngestService", lambda *a, **k: object())
    monkeypatch.setattr(task_module, "ReplyIngestService", lambda *a, **k: object())
    monkeypatch.setattr(task_module, "SqlReportRepository", lambda settings: object())
    monkeypatch.setattr(task_module, "SqlReplyRepository", lambda settings: object())

    settings = SimpleNamespace(redis_control_url="redis://control/2")
    assert await task_module._replay_stale_raw(settings) == 1
    assert [alert["dedup_key"] for alert in alerts] == ["raw_replay_exhausted:1"]
    assert alerts[0]["level"] == "crit"
