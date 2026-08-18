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
async def test_replay_stale_raw_marks_system_producer_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.tasks import reconcile as task_module

    replayed: list[dict[str, object]] = []

    class FakeReplay:
        async def replay(
            self,
            raw_id: int,
            *,
            actor: str,
            ip: str,
            system_producer: bool = False,
        ) -> int:
            replayed.append(
                {
                    "raw_id": raw_id,
                    "actor": actor,
                    "ip": ip,
                    "system_producer": system_producer,
                }
            )
            return 1

    class FakeOps:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def list_stale_unprocessed_raw_ids(self) -> list[int]:
            return [11, 12]

    monkeypatch.setattr(task_module, "redis_client", lambda _url: "redis")
    monkeypatch.setattr(task_module, "SqlOpsRepository", FakeOps)
    monkeypatch.setattr(
        task_module.CryptoService,
        "from_settings",
        lambda settings: "crypto",
    )
    monkeypatch.setattr(task_module, "SqlAlertService", lambda settings: "alerts")
    monkeypatch.setattr(task_module, "SqlReportRepository", lambda settings: "reports")
    monkeypatch.setattr(task_module, "SqlReplyRepository", lambda settings: "replies")
    monkeypatch.setattr(task_module, "ReportIngestService", lambda *a, **k: "report-ingest")
    monkeypatch.setattr(task_module, "ReplyIngestService", lambda *a, **k: "reply-ingest")
    monkeypatch.setattr(task_module, "RawReplayService", lambda *a, **k: FakeReplay())

    count = await task_module._replay_stale_raw(
        SimpleNamespace(redis_control_url="redis://control")
    )

    assert count == 2
    assert replayed == [
        {
            "raw_id": 11,
            "actor": "system-reconcile",
            "ip": "127.0.0.1",
            "system_producer": True,
        },
        {
            "raw_id": 12,
            "actor": "system-reconcile",
            "ip": "127.0.0.1",
            "system_producer": True,
        },
    ]
