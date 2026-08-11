from __future__ import annotations

import base64
import importlib.util
import inspect
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

import app.core.runtime_resources as runtime_resources
from app.core.apikey import ApiAppContext
from app.services.batch_query import BatchAccessScope
from app.services.category import policy_for_category
from app.services.crypto import CryptoService, EncryptionContext, ProtectedPhone
from app.services.export_file import ExportFileCodec
from app.services.housekeeping_repository import SqlHousekeepingRepository
from app.services.idempotency import IdempotencyScope
from app.services.pipeline import PipelineConfig, SendPipeline, SendRequest
from app.services.pipeline_repository import SqlPipelineStore, SqlTemplateRenderer
from app.services.resend import EncryptedFailedPhone, ResendService, ResendSource
from app.services.scheduling_repository import SqlSchedulingRepository

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"
MAILER = ROOT / "deploy" / "scripts" / "send_security_daily_report_resend.py"
LEASE_ID = UUID("20000000-0000-4000-8000-000000000099")


def _crypto(byte: bytes = b"k") -> CryptoService:
    encoded = base64.b64encode(byte * 32).decode()
    return CryptoService.from_secret_values(encoded, encoded)


def _mailer_module() -> ModuleType:
    scripts = str(MAILER.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("security_scan_mailer", MAILER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fingerprint_pipeline(crypto: CryptoService) -> SendPipeline:
    return SendPipeline(
        store=object(),  # type: ignore[arg-type]
        idempotency=object(),  # type: ignore[arg-type]
        crypto=crypto,
        frequency=object(),  # type: ignore[arg-type]
        quota=object(),  # type: ignore[arg-type]
        publisher=object(),  # type: ignore[arg-type]
        config=PipelineConfig(),
    )


def test_idempotency_fingerprint_is_versioned_and_keyed() -> None:
    service = _crypto()
    canonical = b'{"content":"OTP 123456","mobiles":["13800138000"]}'

    digest = service.idempotency_fingerprint(canonical, key_version=1)

    assert digest != __import__("hashlib").sha256(canonical).hexdigest()
    assert len(digest) == 64
    with pytest.raises(ValueError, match="unknown key version"):
        service.idempotency_fingerprint(canonical, key_version=2)


def test_idempotency_fingerprint_survives_rotation_and_distinguishes_equal_masks() -> None:
    first = base64.b64encode(b"1" * 32).decode()
    second = base64.b64encode(b"2" * 32).decode()
    ring = json.dumps(
        {"active_version": 2, "keys": {"1": first, "2": second}}
    )
    crypto = CryptoService.from_secret_values(ring, ring)
    pipeline = _fingerprint_pipeline(crypto)
    app = ApiAppContext(7, "app", "平台部", frozenset({"notice"}))
    policy = policy_for_category("notice", app.allowed_categories)
    first_request = SendRequest(
        "notice",
        (),
        content="通知",
        protected_mobiles=(ProtectedPhone(b"a", "a" * 64, "138****8000", 1),),
        protected_hmac_candidates=((1, "a" * 64), (2, "b" * 64)),
        vendor_test_uat=True,
    )
    second_request = SendRequest(
        "notice",
        (),
        content="通知",
        protected_mobiles=(ProtectedPhone(b"b", "c" * 64, "138****8000", 1),),
        protected_hmac_candidates=((1, "c" * 64), (2, "d" * 64)),
        vendor_test_uat=True,
    )

    first_digest = pipeline._request_hash(
        first_request, app, policy, key_version=1
    )
    assert first_digest == pipeline._request_hash(
        first_request, app, policy, key_version=1
    )
    assert first_digest != pipeline._request_hash(
        second_request, app, policy, key_version=1
    )


def test_template_renderer_requires_authoritative_department() -> None:
    signature = inspect.signature(SqlTemplateRenderer.render)
    source = inspect.getsource(SqlTemplateRenderer.render)

    assert "dept" in signature.parameters
    assert "dept=:dept" in source


@pytest.mark.asyncio
async def test_resend_uses_stable_cross_actor_action_scope() -> None:
    crypto = _crypto(b"r")
    protected = crypto.protect_phone("13800138000")
    content = crypto.encrypt_bound_packed_text(
        "通知",
        EncryptionContext(
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
            object_id="original-1",
        ),
    )

    class Repository:
        async def load(self, batch_no: str, _scope: BatchAccessScope) -> ResendSource:
            assert batch_no == "original-1"
            return ResendSource(
                batch_no="original-1",
                dept="平台部",
                category="notice",
                channel="api",
                send_content_enc=content,
                sign_name=None,
                consent_confirmed=False,
                is_test=False,
                failed_phones=(
                    EncryptedFailedPhone(
                        protected.phone_enc,
                        protected.phone_hmac,
                        protected.key_version,
                    ),
                ),
            )

    request = await ResendService(Repository(), crypto).build_request(
        "original-1", BatchAccessScope(app_id=7)
    )

    assert request.biz_id == "failed-recipients-v1"
    assert IdempotencyScope("resend", "original-1").key == "resend:original-1"
    pipeline = _fingerprint_pipeline(crypto)
    app = ApiAppContext(7, "app", "平台部", frozenset({"notice"}))
    assert pipeline._idempotency_scope(request, app) == IdempotencyScope(
        "resend", "original-1"
    )
    schema = SCHEMA.read_text(encoding="utf-8")
    assert "CREATE TABLE sms_resend_action" in schema
    assert "source_batch_id BIGINT PRIMARY KEY" in schema


def test_live_sms_keeps_idempotency_fact_past_nominal_expiry() -> None:
    pipeline_source = inspect.getsource(SqlPipelineStore)
    housekeeping_source = inspect.getsource(SqlHousekeepingRepository.cleanup)
    scheduling_source = inspect.getsource(SqlSchedulingRepository.reschedule)

    for status in ("pending_approval", "scheduled", "queued", "sending", "balance_blocked"):
        assert status in pipeline_source
        assert status in housekeeping_source
    assert "GREATEST" in scheduling_source
    assert "interval '7 days'" in scheduling_source


def test_callback_role_cannot_read_encrypted_sms_body() -> None:
    schema = SCHEMA.read_text(encoding="utf-8")
    callback_grants = schema.split(
        "-- 回调 worker 的横向影响限制", maxsplit=1
    )[1].split("-- 导出 API/worker", maxsplit=1)[0]

    assert "GRANT SELECT (" in callback_grants
    assert "ON sms_batch TO sms_callback" in callback_grants
    assert "send_content_enc" not in callback_grants


def test_runtime_sqlalchemy_engine_hides_bound_parameters() -> None:
    source = inspect.getsource(runtime_resources.database_engine)
    assert "hide_parameters=True" in source


def test_mailer_rejects_request_bound_to_stale_configuration(tmp_path: Path) -> None:
    module = _mailer_module()
    config_file = tmp_path / "resend.json"
    config_file.write_text(
        json.dumps(
            {
                "api_key": "re_test_value",
                "recipients": ["security@example.com"],
                "config_version": 2,
            }
        ),
        encoding="utf-8",
    )
    configuration = module.read_mailer_configuration(config_file)

    assert configuration.config_version == 2
    assert "config_version" in module.ControlRequest.__dataclass_fields__

    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    request_dir.mkdir(parents=True)
    payload = json.loads(
        (ROOT / "deploy/templates/security_daily_report.sample.json").read_text(
            encoding="utf-8"
        )
    )
    request_path = request_dir / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "request_id": "20000000-0000-4000-8000-000000000001",
                "report_date": payload["report_date"],
                "action": "send",
                "config_version": 1,
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )

    class NoNetwork:
        calls = 0

        def post(self, **_values: object) -> tuple[int, bytes]:
            self.calls += 1
            return 200, b'{"id":"unexpected"}'

    transport = NoNetwork()
    state = module.process_control_request(
        request_path,
        control_dir=control_dir,
        config_file=config_file,
        transport=transport,
    )

    assert state == "failed"
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_export_verifies_terminal_frame_before_first_plaintext(
    tmp_path: Path,
) -> None:
    codec = ExportFileCodec(_crypto(b"e"), tmp_path, frame_size=32)

    async def rows() -> AsyncIterator[tuple[str, ...]]:
        yield ("13800138000", "通知")
        yield ("13900139000", "第二条通知")

    path = await codec.write_csv(9, LEASE_ID, ("phone", "content"), rows())
    path.write_bytes(path.read_bytes()[:-3])

    stream = codec.iter_decrypted(path)
    with pytest.raises(ValueError, match="truncated"):
        next(stream)
