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

    uat_ran = {"value": False}

    class ContinuingUatRecovery:
        def __init__(self, repository: object) -> None:
            pass

        async def reconcile_once(self) -> int:
            uat_ran["value"] = True
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
    monkeypatch.setattr(
        task_module, "VendorTestUatReconciler", ContinuingUatRecovery, raising=False
    )

    alerts: list[dict[str, object]] = []

    class Alerts:
        def __init__(self, settings: object) -> None:
            assert settings == "settings"

        async def emit(self, **kwargs: object) -> None:
            alerts.append(kwargs)

    monkeypatch.setattr(task_module, "SqlAlertService", Alerts)

    async def replay_none(_settings: object) -> int:
        return 0

    monkeypatch.setattr(task_module, "_replay_stale_raw", replay_none)

    with pytest.raises(
        task_module.ReconcilePartialFailure,
        match="reconcile domains failed: 1",
    ) as captured:
        await task_module._reconcile()

    assert uat_ran["value"] is True
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert "private-finalizer-detail" in str(captured.value.__cause__)
    assert "private-finalizer-detail" not in str(captured.value)
    assert alerts[0]["alert_type"] == "reconcile_domain_failed"
    assert alerts[0]["detail"] == {
        "domain": "vendor-control",
        "error_type": "RuntimeError",
    }
    assert alerts[0]["dedup_key"] == "reconcile_domain_failed:vendor-control"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_domain",
    (
        "uncertain",
        "delivery-recovery",
        "vendor-control",
        "vendor-uat",
        "raw-replay",
    ),
)
async def test_reconcile_task_isolates_each_domain_failure(
    monkeypatch: pytest.MonkeyPatch,
    failed_domain: str,
) -> None:
    from app.tasks import reconcile as task_module

    ran: list[str] = []
    alerts: list[dict[str, object]] = []

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
            ran.append("uncertain")
            if failed_domain == "uncertain":
                raise RuntimeError("uncertain-domain-fault")
            return 1

    class Recovery:
        def __init__(self, repository: object, publisher: object) -> None:
            pass

        async def run_once(self) -> int:
            ran.append("delivery-recovery")
            if failed_domain == "delivery-recovery":
                raise RuntimeError("recovery-domain-fault")
            return 2

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
            ran.append("vendor-control")
            if failed_domain == "vendor-control":
                raise RuntimeError("control-domain-fault")
            return 3

    class UatRecovery:
        def __init__(self, repository: object) -> None:
            pass

        async def reconcile_once(self) -> int:
            ran.append("vendor-uat")
            if failed_domain == "vendor-uat":
                raise RuntimeError("uat-domain-fault")
            return 4

    class Alerts:
        def __init__(self, settings: object) -> None:
            pass

        async def emit(self, **kwargs: object) -> None:
            alerts.append(kwargs)

    async def replay(_settings: object) -> int:
        ran.append("raw-replay")
        if failed_domain == "raw-replay":
            raise RuntimeError("raw-replay-domain-fault")
        return 5

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
    monkeypatch.setattr(task_module, "SqlAlertService", Alerts)
    monkeypatch.setattr(task_module, "_replay_stale_raw", replay)

    with pytest.raises(task_module.ReconcilePartialFailure, match="reconcile domains failed: 1"):
        await task_module._reconcile()

    assert ran == [
        "uncertain",
        "delivery-recovery",
        "vendor-control",
        "vendor-uat",
        "raw-replay",
    ]
    assert [alert["detail"] for alert in alerts] == [
        {"domain": failed_domain, "error_type": "RuntimeError"}
    ]
    assert "fault" not in str(alerts)


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

        async def raw_replay_exhausted(self, raw_id: int) -> bool:
            return False

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

        async def replay(
            self,
            raw_id: int,
            *,
            actor: str,
            ip: str,
            system_producer: bool = False,
        ) -> int:
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
