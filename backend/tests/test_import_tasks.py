from __future__ import annotations

import inspect
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

import app.tasks.imports as imports_module
from app.services.import_repository import SqlImportRepository
from app.services.imports import RemovedPhone
from app.tasks import TRANSIENT_TASK_ERRORS, celery_app, register_task_modules
from app.tasks.imports import ImportSender, dispatch_imports_once, process_import_once
from app.tasks.scheduler import build_beat_schedule


class FakeRepository:
    async def pending_parse_ids(self) -> list[str]:
        return ["import-1", "import-2"]


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, import_id: str) -> None:
        self.sent.append(import_id)


@pytest.mark.asyncio
async def test_dispatcher_republishes_pending_and_expired_import_leases() -> None:
    sender = FakeSender()

    count = await dispatch_imports_once(
        cast(SqlImportRepository, FakeRepository()),
        cast(ImportSender, sender),
    )

    assert count == 2
    assert sender.sent == ["import-1", "import-2"]


def test_import_tasks_have_crash_recovery_and_bounded_worker_settings() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.worker_max_tasks_per_child == 200
    assert celery_app.conf.worker_max_memory_per_child == 512_000
    assert celery_app.conf.broker_use_ssl is False
    assert celery_app.conf.redis_backend_use_ssl is None
    task = celery_app.tasks["app.tasks.process_import"]
    assert task.soft_time_limit == 120
    assert task.time_limit == 150
    assert build_beat_schedule({})["dispatch-imports"] == {
        "task": "app.tasks.dispatch_imports",
        "schedule": 30,
        "options": {"queue": "bulk"},
    }


def test_production_celery_redis_transport_requires_verified_tls(
    tmp_path: Path,
) -> None:
    ca_file = tmp_path / "redis-ca.pem"
    ldap_ca_file = tmp_path / "ldap-ca.pem"
    broker_secret = tmp_path / "broker"
    auth_secret = tmp_path / "auth"
    control_secret = tmp_path / "control"
    for path, value in (
        (ca_file, "redis-ca"),
        (ldap_ca_file, "ldap-ca"),
        (broker_secret, "broker-pass"),
        (auth_secret, "auth-pass"),
        (control_secret, "control-pass"),
    ):
        path.write_text(value, encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "ENVIRONMENT": "production",
            "SMS_COMPONENT": "worker",
            "DEBUG": "0",
            "AUTH_MOCK": "0",
            "VENDOR_MOCK": "0",
            "VENDOR_BASE_URL": "https://vendor.example.test",
            "REDIS_HA_MODE": "managed",
            "REDIS_CA_CERTS_FILE": str(ca_file),
            "LDAP_CA_CERTS_FILE": str(ldap_ca_file),
            "REDIS_BROKER_PASSWORD_FILE": str(broker_secret),
            "REDIS_AUTH_PASSWORD_FILE": str(auth_secret),
            "REDIS_CONTROL_PASSWORD_FILE": str(control_secret),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import ssl; from app.tasks import celery_app; "
                "expected={'ssl_ca_certs':r'"
                + str(ca_file)
                + "','ssl_cert_reqs':ssl.CERT_REQUIRED,'ssl_check_hostname':True}; "
                "assert celery_app.conf.broker_use_ssl == expected; "
                "assert celery_app.conf.redis_backend_use_ssl == expected"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).parents[1],
        env=env,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr


def test_background_tasks_retry_only_transient_failures_with_hard_bounds() -> None:
    register_task_modules()

    expected_limits = {
        "app.tasks.process_import": (120, 150),
        "app.tasks.dispatch_imports": (120, 150),
        "app.tasks.anomaly_scan": (120, 150),
        "app.tasks.dispatch_exports": (120, 150),
        "app.tasks.cleanup_exports": (300, 360),
        "app.tasks.housekeeping": (900, 960),
    }
    for name, (soft_limit, hard_limit) in expected_limits.items():
        task = celery_app.tasks[name]
        assert task.autoretry_for == TRANSIENT_TASK_ERRORS
        assert task.retry_backoff is True
        assert task.retry_backoff_max == 300
        assert task.retry_jitter is True
        assert task.max_retries == 3
        assert task.soft_time_limit == soft_limit
        assert task.time_limit == hard_limit

    from sqlalchemy.exc import ProgrammingError

    assert ProgrammingError not in TRANSIENT_TASK_ERRORS


@pytest.mark.asyncio
async def test_process_import_streams_chunks_and_cleans_encrypted_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import_id = uuid4()
    lease_id = uuid4()
    claim = SimpleNamespace(
        import_id=import_id,
        lease_id=lease_id,
        source_file="source.enc",
        source_size=64,
        filename="phones.csv",
    )
    accepted_phone = SimpleNamespace(
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        source_row=1,
    )
    blocked_phone = SimpleNamespace(
        phone_hmac="b" * 64,
        phone_mask="139****9000",
        source_row=2,
    )
    parse_chunk = SimpleNamespace(
        candidates_by_active={
            accepted_phone.phone_hmac: {"a" * 64},
            blocked_phone.phone_hmac: {"b" * 64},
        },
        valid=(accepted_phone, blocked_phone),
        removed=(RemovedPhone("137****7000", 3, "invalid"),),
    )

    class Repository:
        def __init__(self) -> None:
            self.appended: list[tuple[Any, ...]] = []
            self.finished: dict[str, Any] | None = None
            self.cleared = False

        async def claim_parse(self, requested: str) -> Any:
            assert requested == str(import_id)
            return claim

        async def append_parse_batch(
            self,
            actual_claim: Any,
            phones: tuple[Any, ...],
        ) -> bool:
            assert actual_claim is claim
            self.appended.append(phones)
            return True

        async def finish_parse(self, actual_claim: Any, **values: Any) -> bool:
            assert actual_claim is claim
            self.finished = values
            return True

        async def clear_source(self, actual_claim: Any) -> None:
            assert actual_claim is claim
            self.cleared = True

    class Redis:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Codec:
        def __init__(self) -> None:
            self.removed: list[str] = []

        def decrypt_to_memory(self, *args: Any, **kwargs: Any) -> BytesIO:
            return BytesIO(b"encrypted-test-source")

        def remove(self, name: str) -> None:
            self.removed.append(name)

    class Parser:
        def __init__(self) -> None:
            self.blacklist = self

        def iter_chunks(self, *args: Any, **kwargs: Any) -> Any:
            return iter((parse_chunk,))

        async def matches(self, candidates: set[str]) -> set[str]:
            assert candidates == {"a" * 64, "b" * 64}
            return {"b" * 64}

    repository = Repository()
    redis = Redis()
    codec = Codec()
    parser = Parser()
    settings = SimpleNamespace(
        redis_control_url="redis://synthetic",
        import_storage_dir=tmp_path,
    )

    async def immediate(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_s", None)
        result = function(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    monkeypatch.setattr(imports_module, "get_settings", lambda: settings)
    monkeypatch.setattr(imports_module, "SqlImportRepository", lambda _settings: repository)
    monkeypatch.setattr(
        imports_module.CryptoService,
        "from_settings",
        lambda _settings: object(),
    )
    async def load_policy() -> object:
        return object()

    monkeypatch.setattr(
        imports_module,
        "SqlRuntimePolicyLoader",
        lambda *_args, **_kwargs: SimpleNamespace(load=load_policy),
    )
    monkeypatch.setattr(
        imports_module.ImportLimits,
        "from_policy",
        lambda _policy: SimpleNamespace(max_bytes=1024),
    )
    monkeypatch.setattr(imports_module, "ImportFileCodec", lambda *_args: codec)
    monkeypatch.setattr(imports_module.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(imports_module, "ImportParser", lambda *_args, **_kwargs: parser)
    monkeypatch.setattr(imports_module, "run_bounded", immediate)

    assert await process_import_once(str(import_id)) == 1
    assert repository.appended == [(accepted_phone,)]
    assert repository.finished == {
        "valid": 1,
        "invalid": 1,
        "duplicate": 0,
        "blacklisted": 1,
        "invalid_file": f"removed-{import_id}-{lease_id}.csv",
    }
    assert repository.cleared
    assert codec.removed == ["source.enc"]
    assert redis.closed
    removed_file = tmp_path / f"removed-{import_id}-{lease_id}.csv"
    removed_text = removed_file.read_text(encoding="utf-8")
    assert "138****8000" not in removed_text
    assert "139****9000" in removed_text
    assert "137****7000" in removed_text
