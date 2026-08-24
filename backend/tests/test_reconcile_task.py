from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable
from time import monotonic
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.runtime_policy import InvalidRuntimePolicy, RuntimePolicy

RECONCILE_DOMAINS = (
    "uncertain",
    "delivery-recovery",
    "vendor-control",
    "vendor-uat",
    "raw-replay",
)
PREFLIGHT_LEAK = "preflight-secret-value-must-not-leak"
CORRUPT_POLICY_VALUE = "not-a-cidr;token=leak-me-not-policy"


class _ReconcileProbe:
    def __init__(self) -> None:
        self.succeeded: list[str] = []
        self.succeeded_at: dict[str, list[float]] = defaultdict(list)
        self.alerts: list[dict[str, object]] = []

    def mark(self, domain: str) -> None:
        self.succeeded.append(domain)
        self.succeeded_at[domain].append(monotonic())


def _raise_preflight(
    domain: str,
    site: str,
    failed_domain: str | None,
    failed_site: str | None,
    error_factory: Callable[[], BaseException] | None,
) -> None:
    if failed_domain == domain and failed_site == site:
        assert error_factory is not None
        raise error_factory()


def _install_reconcile_probe(
    monkeypatch: pytest.MonkeyPatch,
    task_module: Any,
    *,
    failed_domain: str | None = None,
    failed_site: str | None = None,
    error_factory: Callable[[], BaseException] | None = None,
    policy_loader: type[Any] | None = None,
) -> _ReconcileProbe:
    """安装五域探测桩；raw-replay 走真实函数以便注入其仓储/Redis/密钥前置。"""

    probe = _ReconcileProbe()
    crypto_calls = {"n": 0}
    settings = SimpleNamespace(redis_control_url="redis://control")

    class DefaultPolicyLoader:
        def __init__(self, loaded_settings: object) -> None:
            assert loaded_settings is settings

        async def load(self) -> object:
            if failed_site in {"policy_invalid", "policy_db_read"}:
                _raise_preflight(
                    "uncertain",
                    failed_site,
                    failed_domain,
                    failed_site,
                    error_factory,
                )
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
            probe.mark("uncertain")
            return 1

    class UncertainRepo:
        def __init__(self, loaded_settings: object) -> None:
            _raise_preflight(
                "uncertain", "repo_init", failed_domain, failed_site, error_factory
            )

    class Recovery:
        def __init__(self, repository: object, publisher: object) -> None:
            pass

        async def run_once(self) -> int:
            probe.mark("delivery-recovery")
            return 2

    class RecoveryRepo:
        def __init__(self, loaded_settings: object) -> None:
            _raise_preflight(
                "delivery-recovery", "repo_init", failed_domain, failed_site, error_factory
            )

    class Publisher:
        def __init__(self) -> None:
            _raise_preflight(
                "delivery-recovery",
                "publisher_init",
                failed_domain,
                failed_site,
                error_factory,
            )

    class Operations:
        def __init__(
            self,
            repository: object,
            client: object,
            *,
            finalizers: dict[str, object],
        ) -> None:
            _raise_preflight(
                "vendor-control", "repo_init", failed_domain, failed_site, error_factory
            )

        async def reconcile_once(self) -> int:
            probe.mark("vendor-control")
            return 3

    class ControlClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _raise_preflight(
                "vendor-control", "client_init", failed_domain, failed_site, error_factory
            )

    class UatRecovery:
        def __init__(self, repository: object) -> None:
            _raise_preflight(
                "vendor-uat", "repo_init", failed_domain, failed_site, error_factory
            )

        async def reconcile_once(self) -> int:
            probe.mark("vendor-uat")
            return 4

    class SharedVendorRepo:
        def __init__(self, loaded_settings: object) -> None:
            _raise_preflight(
                "vendor-control",
                "shared_repo_init",
                failed_domain,
                failed_site,
                error_factory,
            )

    class Alerts:
        def __init__(self, loaded_settings: object) -> None:
            pass

        async def emit(self, **kwargs: object) -> None:
            probe.alerts.append(kwargs)

    class Ops:
        def __init__(self, loaded_settings: object, redis: object) -> None:
            _raise_preflight(
                "raw-replay", "repo_init", failed_domain, failed_site, error_factory
            )

        async def list_stale_unprocessed_raw_ids(self) -> list[int]:
            return [11]

        async def list_pending_system_replay_audit_ids(self) -> list[int]:
            return []

        async def raw_replay_exhausted(self, raw_id: int) -> bool:
            return False

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
            return 1

    def from_settings(loaded_settings: object) -> object:
        crypto_calls["n"] += 1
        if (
            failed_domain == "uncertain"
            and failed_site == "crypto_secret"
            and crypto_calls["n"] == 1
        ):
            assert error_factory is not None
            raise error_factory()
        if (
            failed_domain == "raw-replay"
            and failed_site == "crypto_secret"
            and crypto_calls["n"] >= 2
        ):
            assert error_factory is not None
            raise error_factory()
        return "crypto"

    def redis_client(url: object) -> object:
        _raise_preflight(
            "raw-replay", "redis_client", failed_domain, failed_site, error_factory
        )
        return "redis"

    original_replay = task_module._replay_stale_raw

    async def tracked_replay(loaded_settings: object) -> int:
        count = await original_replay(loaded_settings)
        probe.mark("raw-replay")
        return count

    monkeypatch.setattr(task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(task_module.CryptoService, "from_settings", from_settings)
    monkeypatch.setattr(
        task_module, "SqlRuntimePolicyLoader", policy_loader or DefaultPolicyLoader
    )
    monkeypatch.setattr(task_module, "UncertainReconciler", Uncertain)
    monkeypatch.setattr(task_module, "SqlUncertainRepository", UncertainRepo)
    monkeypatch.setattr(task_module, "RecoveryReconciler", Recovery)
    monkeypatch.setattr(task_module, "SqlRecoveryRepository", RecoveryRepo)
    monkeypatch.setattr(task_module, "CeleryQueuePublisher", Publisher)
    monkeypatch.setattr(task_module, "VendorTestOperationService", Operations)
    monkeypatch.setattr(task_module, "VendorControlClient", ControlClient)
    monkeypatch.setattr(task_module, "VendorTestUatReconciler", UatRecovery, raising=False)
    monkeypatch.setattr(task_module, "SqlVendorTestOperationRepository", SharedVendorRepo)
    monkeypatch.setattr(task_module, "SqlAlertService", Alerts)
    monkeypatch.setattr(task_module, "redis_client", redis_client)
    monkeypatch.setattr(task_module, "SqlOpsRepository", Ops)
    monkeypatch.setattr(task_module, "RawReplayService", Replay)
    monkeypatch.setattr(task_module, "ReportIngestService", lambda *a, **k: object())
    monkeypatch.setattr(task_module, "ReplyIngestService", lambda *a, **k: object())
    monkeypatch.setattr(task_module, "SqlReportRepository", lambda loaded: object())
    monkeypatch.setattr(task_module, "SqlReplyRepository", lambda loaded: object())
    monkeypatch.setattr(task_module, "_replay_stale_raw", tracked_replay)
    return probe


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

        async def list_pending_system_replay_audit_ids(self) -> list[int]:
            return []

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
async def test_replay_stale_raw_emits_system_audit_gap_instead_of_silent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.services.raw_replay import RawReplaySystemAuditIncomplete
    from app.tasks import reconcile as task_module

    alerts: list[dict[str, object]] = []

    class FakeAlerts:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def emit(self, **kwargs: object) -> None:
            alerts.append(kwargs)

    class FakeReplay:
        async def replay(self, raw_id: int, **_: object) -> int:
            raise RawReplaySystemAuditIncomplete(raw_id, 3)

    class FakeOps:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def list_stale_unprocessed_raw_ids(self) -> list[int]:
            return [21]

        async def list_pending_system_replay_audit_ids(self) -> list[int]:
            return [22]

        async def raw_replay_exhausted(self, raw_id: int) -> bool:
            raise AssertionError("audit gap must not use replay_exhausted")

    monkeypatch.setattr(task_module, "redis_client", lambda _url: "redis")
    monkeypatch.setattr(task_module, "SqlOpsRepository", FakeOps)
    monkeypatch.setattr(
        task_module.CryptoService, "from_settings", lambda settings: "crypto"
    )
    monkeypatch.setattr(task_module, "SqlAlertService", FakeAlerts)
    monkeypatch.setattr(task_module, "SqlReportRepository", lambda settings: "reports")
    monkeypatch.setattr(task_module, "SqlReplyRepository", lambda settings: "replies")
    monkeypatch.setattr(task_module, "ReportIngestService", lambda *a, **k: "report-ingest")
    monkeypatch.setattr(task_module, "ReplyIngestService", lambda *a, **k: "reply-ingest")
    monkeypatch.setattr(task_module, "RawReplayService", lambda *a, **k: FakeReplay())

    count = await task_module._replay_stale_raw(
        SimpleNamespace(redis_control_url="redis://control")
    )

    assert count == 0
    assert [item["alert_type"] for item in alerts] == [
        "raw_system_audit_gap",
        "raw_system_audit_gap",
    ]
    assert alerts[0]["detail"] == {"raw_id": 21, "lease_epoch": 3}
    assert "phone" not in str(alerts).lower()


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

        async def list_pending_system_replay_audit_ids(self) -> list[int]:
            return []

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


def test_reconcile_loads_policy_only_inside_uncertain_domain() -> None:
    from app.tasks import reconcile as task_module

    source = inspect.getsource(task_module._reconcile)
    assert source.index("get_settings()") < source.index("async def uncertain")
    assert source.index("async def uncertain") < source.index("SqlRuntimePolicyLoader")
    assert source.index("SqlRuntimePolicyLoader") < source.index("for name, operation")
    assert source.index("SqlVendorTestOperationRepository") > source.index(
        "async def vendor_control"
    )
    assert source.index("VendorControlClient") > source.index("async def vendor_control")
    assert source.index("CryptoService.from_settings") > source.index("async def uncertain")


@pytest.mark.asyncio
@pytest.mark.fault_injection
@pytest.mark.parametrize(
    ("failed_domain", "failed_site", "error_type", "error_factory"),
    (
        pytest.param(
            "uncertain",
            "policy_invalid",
            "InvalidRuntimePolicy",
            lambda: InvalidRuntimePolicy(
                f"callback_allow_cidrs rejected {PREFLIGHT_LEAK}"
            ),
            id="uncertain-policy-invalid",
        ),
        pytest.param(
            "uncertain",
            "policy_db_read",
            "RuntimeError",
            lambda: RuntimeError(f"sys_config read failed {PREFLIGHT_LEAK}"),
            id="uncertain-policy-db-read",
        ),
        pytest.param(
            "uncertain",
            "crypto_secret",
            "OSError",
            lambda: OSError(f"data_aes_key unreadable {PREFLIGHT_LEAK}"),
            id="uncertain-crypto-secret",
        ),
        pytest.param(
            "uncertain",
            "repo_init",
            "RuntimeError",
            lambda: RuntimeError(f"uncertain repository init failed {PREFLIGHT_LEAK}"),
            id="uncertain-repo-init",
        ),
        pytest.param(
            "delivery-recovery",
            "repo_init",
            "RuntimeError",
            lambda: RuntimeError(f"recovery repository init failed {PREFLIGHT_LEAK}"),
            id="delivery-repo-init",
        ),
        pytest.param(
            "delivery-recovery",
            "publisher_init",
            "RuntimeError",
            lambda: RuntimeError(f"queue publisher init failed {PREFLIGHT_LEAK}"),
            id="delivery-publisher-init",
        ),
        pytest.param(
            "vendor-control",
            "repo_init",
            "RuntimeError",
            lambda: RuntimeError(f"control repository init failed {PREFLIGHT_LEAK}"),
            id="control-repo-init",
        ),
        pytest.param(
            "vendor-control",
            "client_init",
            "RuntimeError",
            lambda: RuntimeError(f"vendor control client init failed {PREFLIGHT_LEAK}"),
            id="control-client-init",
        ),
        pytest.param(
            "vendor-uat",
            "repo_init",
            "RuntimeError",
            lambda: RuntimeError(f"uat repository init failed {PREFLIGHT_LEAK}"),
            id="uat-repo-init",
        ),
        pytest.param(
            "raw-replay",
            "redis_client",
            "RuntimeError",
            lambda: RuntimeError(f"control redis init failed {PREFLIGHT_LEAK}"),
            id="raw-redis-client",
        ),
        pytest.param(
            "raw-replay",
            "repo_init",
            "RuntimeError",
            lambda: RuntimeError(f"ops repository init failed {PREFLIGHT_LEAK}"),
            id="raw-repo-init",
        ),
        pytest.param(
            "raw-replay",
            "crypto_secret",
            "OSError",
            lambda: OSError(f"data_hmac_key unreadable {PREFLIGHT_LEAK}"),
            id="raw-crypto-secret",
        ),
    ),
)
async def test_reconcile_preflight_fault_matrix_isolates_each_domain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_domain: str,
    failed_site: str,
    error_type: str,
    error_factory: Callable[[], BaseException],
) -> None:
    from app.tasks import reconcile as task_module

    probe = _install_reconcile_probe(
        monkeypatch,
        task_module,
        failed_domain=failed_domain,
        failed_site=failed_site,
        error_factory=error_factory,
    )

    with pytest.raises(
        task_module.ReconcilePartialFailure,
        match="reconcile domains failed: 1",
    ) as captured:
        await task_module._reconcile()

    assert probe.succeeded == [domain for domain in RECONCILE_DOMAINS if domain != failed_domain]
    assert [alert["detail"] for alert in probe.alerts] == [
        {"domain": failed_domain, "error_type": error_type}
    ]
    assert probe.alerts[0]["alert_type"] == "reconcile_domain_failed"
    assert probe.alerts[0]["dedup_key"] == f"reconcile_domain_failed:{failed_domain}"
    assert PREFLIGHT_LEAK not in str(captured.value)
    assert PREFLIGHT_LEAK in str(captured.value.__cause__)
    assert PREFLIGHT_LEAK not in caplog.text
    assert PREFLIGHT_LEAK not in repr(probe.alerts)


@pytest.mark.asyncio
@pytest.mark.fault_injection
async def test_corrupt_runtime_policy_does_not_block_delivery_raw_uat(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.tasks import reconcile as task_module

    class CorruptPolicyLoader:
        def __init__(self, settings: object) -> None:
            pass

        async def load(self) -> object:
            return RuntimePolicy.from_mapping(
                {"callback_allow_cidrs": CORRUPT_POLICY_VALUE}
            )

    probe = _install_reconcile_probe(
        monkeypatch,
        task_module,
        policy_loader=CorruptPolicyLoader,
    )

    with pytest.raises(
        task_module.ReconcilePartialFailure,
        match="reconcile domains failed: 1",
    ) as captured:
        await task_module._reconcile()

    assert probe.succeeded == [
        "delivery-recovery",
        "vendor-control",
        "vendor-uat",
        "raw-replay",
    ]
    assert isinstance(captured.value.__cause__, InvalidRuntimePolicy)
    assert probe.alerts[0]["detail"] == {
        "domain": "uncertain",
        "error_type": "InvalidRuntimePolicy",
    }
    assert CORRUPT_POLICY_VALUE not in str(captured.value)
    assert CORRUPT_POLICY_VALUE not in caplog.text
    assert CORRUPT_POLICY_VALUE not in repr(probe.alerts)
    assert CORRUPT_POLICY_VALUE not in str(captured.value.__cause__)


@pytest.mark.asyncio
@pytest.mark.fault_injection
async def test_policy_db_read_failure_still_runs_raw_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import reconcile as task_module

    probe = _install_reconcile_probe(
        monkeypatch,
        task_module,
        failed_domain="uncertain",
        failed_site="policy_db_read",
        error_factory=lambda: RuntimeError(f"sys_config unavailable {PREFLIGHT_LEAK}"),
    )

    with pytest.raises(task_module.ReconcilePartialFailure, match="reconcile domains failed: 1"):
        await task_module._reconcile()

    assert "raw-replay" in probe.succeeded
    assert "uncertain" not in probe.succeeded


@pytest.mark.asyncio
@pytest.mark.fault_injection
async def test_shared_vendor_repo_init_failure_still_runs_other_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import reconcile as task_module

    probe = _install_reconcile_probe(
        monkeypatch,
        task_module,
        failed_domain="vendor-control",
        failed_site="shared_repo_init",
        error_factory=lambda: RuntimeError(f"shared operation repo init {PREFLIGHT_LEAK}"),
    )

    with pytest.raises(
        task_module.ReconcilePartialFailure,
        match="reconcile domains failed: 2",
    ) as captured:
        await task_module._reconcile()

    assert probe.succeeded == ["uncertain", "delivery-recovery", "raw-replay"]
    assert [alert["detail"]["domain"] for alert in probe.alerts] == [
        "vendor-control",
        "vendor-uat",
    ]
    assert PREFLIGHT_LEAK not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.fault_injection
async def test_persistent_uncertain_preflight_does_not_starve_other_domain_rto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import reconcile as task_module

    probe = _install_reconcile_probe(
        monkeypatch,
        task_module,
        failed_domain="uncertain",
        failed_site="policy_invalid",
        error_factory=lambda: InvalidRuntimePolicy(
            f"callback_allow_cidrs rejected {PREFLIGHT_LEAK}"
        ),
    )

    with pytest.raises(task_module.ReconcilePartialFailure, match="reconcile domains failed: 1"):
        await task_module._reconcile()
    first_success = {
        domain: probe.succeeded_at[domain][-1]
        for domain in RECONCILE_DOMAINS
        if domain != "uncertain"
    }

    with pytest.raises(task_module.ReconcilePartialFailure, match="reconcile domains failed: 1"):
        await task_module._reconcile()

    for domain, first_at in first_success.items():
        assert probe.succeeded_at[domain] == sorted(probe.succeeded_at[domain])
        assert len(probe.succeeded_at[domain]) == 2
        assert probe.succeeded_at[domain][1] > first_at
    assert probe.succeeded_at["uncertain"] == []
    assert [alert["detail"]["domain"] for alert in probe.alerts] == ["uncertain", "uncertain"]
