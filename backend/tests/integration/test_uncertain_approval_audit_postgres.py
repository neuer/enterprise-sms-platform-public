"""正式发送角色覆盖审批阈值、真实审计主体与人工终态的 Outbox 收尾。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth.accounts import UncertainEffectPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services.crypto import CryptoService
from app.services.outbox_repository import SqlOutboxRepository
from app.services.uncertain_resolution import UncertainResolutionConflict
from tests.integration.test_uncertain_web_usage_subject_postgres import (
    _apply_as_send,
    _approve,
    _crypto,
    _insert_unknown,
    _NoDisposeEngine,
    _phone,
    _send_runtime,
)
from tests.integration.test_uncertain_web_usage_subject_postgres import env as env
from tests.integration.test_uncertain_web_usage_subject_postgres import pytestmark as pytestmark


async def _source(env: Any, count: int) -> tuple[int, int]:
    """源明细逐个加密；数量跨越实际默认审批阈值，不调低阈值来替代边界。"""

    batch, chunk = await _insert_unknown(
        env.engine, _crypto(), channel="web", dept="运营一部", app_id=None,
        phone=_phone(env.nonce, 1),
    )
    async with env.engine.begin() as connection:
        for index in range(2, count + 1):
            phone = _crypto().protect_phone(_phone(env.nonce, index))
            await connection.execute(text("""
                INSERT INTO sms_message(batch_id,chunk_id,phone_enc,phone_hmac,
                  phone_mask,key_version,status)
                VALUES(:batch,:chunk,:enc,:hmac,:mask,:version,'unknown')
            """), {"batch": batch, "chunk": chunk, "enc": phone.phone_enc,
                    "hmac": phone.phone_hmac, "mask": phone.phone_mask,
                    "version": phone.key_version})
        await connection.execute(text("""
            UPDATE sms_batch SET total=:count,unknown_cnt=:count,quota_cost=:count WHERE id=:id
        """), {"id": batch, "count": count})
        await connection.execute(text("UPDATE sms_chunk SET phone_count=:count WHERE id=:id"),
                                 {"id": chunk, "count": count})
    return batch, chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [99, 100, 101])
async def test_web_resend_keeps_threshold_and_stable_confirmation_applicant(
    env: Any, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    _batch, chunk = await _source(env, count)
    resolution = await _approve(env.service, chunk, env.proposer, env.confirmer)
    runtime = await _send_runtime(env, monkeypatch)
    try:
        result = await _apply_as_send(runtime, resolution, monkeypatch)
        assert result.state == "closed" and result.child_batch_id
        again = await _apply_as_send(runtime, resolution, monkeypatch)
        assert again.child_batch_id == result.child_batch_id
        async with env.engine.connect() as connection:
            child = (await connection.execute(text("""
                SELECT status,total,creator,creator_account_id,creator_identity_id
                FROM sms_batch WHERE id=:id
            """), {"id": result.child_batch_id})).mappings().one()
            assert child["status"] == ("queued" if count < 100 else "pending_approval")
            assert child["total"] == count
            assert child["creator"] == f"system_resend:{resolution}"
            assert child["creator_account_id"] is None and child["creator_identity_id"] is None
            approval = (await connection.execute(text("""
                SELECT applicant,applicant_account_id,applicant_identity_id,trigger_threshold
                FROM approval WHERE batch_id=:id
            """), {"id": result.child_batch_id})).mappings().one_or_none()
            if count < 100:
                assert approval is None
            else:
                assert dict(approval) == {
                    "applicant": env.confirmer.login_name,
                    "applicant_account_id": env.confirmer.account_id,
                    "applicant_identity_id": env.confirmer.identity_id,
                    "trigger_threshold": 100,
                }
                assert await connection.scalar(text("""
                    SELECT count(*) FROM outbox_event
                    WHERE event_type='batch.ready' AND aggregate_id=(
                      SELECT trim(batch_no) FROM sms_batch WHERE id=:id)
                """), {"id": result.child_batch_id}) == 0
            assert await connection.scalar(text("SELECT status FROM sms_chunk WHERE id=:id"),
                                           {"id": chunk}) == "unknown_terminal"
            assert await connection.scalar(text("""
                SELECT count(*) FROM sms_uncertain_child WHERE resolution_id=:id
            """), {"id": resolution}) == 1
        if count >= 100:
            with pytest.raises(DBAPIError, match="chk_no_self_approve"):
                async with env.engine.begin() as connection:
                    await connection.execute(text("""
                        UPDATE approval SET approver_account_id=applicant_account_id,
                          approver_identity_id=applicant_identity_id WHERE batch_id=:id
                    """), {"id": result.child_batch_id})
        async with runtime.engine.connect() as connection:
            assert not await connection.scalar(text(
                "SELECT has_table_privilege(current_user,'audit_log','SELECT')"
            ))
    finally:
        await runtime.engine.dispose()


@pytest.mark.asyncio
async def test_internal_sensitive_audit_uses_verified_system_subject(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _batch, chunk = await _source(env, 1)
    resolution = await _approve(env.service, chunk, env.proposer, env.confirmer)
    runtime = await _send_runtime(env, monkeypatch)
    runtime.pipeline.config = replace(runtime.pipeline.config, sensitive_hit_action="audit")
    runtime.pipeline.store.sensitive = True
    try:
        result = await _apply_as_send(runtime, resolution, monkeypatch)
        assert result.state == "closed"
        async with env.engine.connect() as connection:
            row = (await connection.execute(text("""
                SELECT actor_subject_kind,actor_account_id,actor_identity_id,actor_app_id,after_val
                FROM audit_log WHERE action='sensitive_hit' AND actor=:actor
            """), {"actor": f"system_resend:{resolution}"})).mappings().one()
            assert dict(row) == {
                "actor_subject_kind": "system", "actor_account_id": None,
                "actor_identity_id": None, "actor_app_id": None,
                "after_val": {"hit_count": 1, "effect_generation": 1},
            }
        principal = UncertainEffectPrincipal(
            resolution, env.proposer.account_id, env.confirmer.account_id, 1, "运营一部",
        )
        with (
            correlation_scope(), audit_principal_scope(),
            pytest.raises(UncertainResolutionConflict),
        ):
            await runtime.pipeline.store.audit_sensitive_hit(principal, 1)
    finally:
        await runtime.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [False, True])
async def test_outbox_acknowledges_only_persisted_manual_terminal(
    env: Any, monkeypatch: pytest.MonkeyPatch, transient: bool,
) -> None:
    import app.services.uncertain_resolution as module
    import app.tasks.outbox as tasks

    _batch, chunk = await _source(env, 1)
    resolution = await _approve(env.service, chunk, env.proposer, env.confirmer)
    runtime = await _send_runtime(env, monkeypatch)
    class Repository(SqlOutboxRepository):
        def _engine(self) -> Any:
            return _NoDisposeEngine(runtime.engine)
    monkeypatch.setattr(tasks, "SqlOutboxRepository", lambda _settings: Repository(env.settings))
    monkeypatch.setattr(module, "UncertainResolutionService", lambda _crypto: runtime.service)
    monkeypatch.setattr(
        CryptoService, "from_settings", classmethod(lambda cls, settings: _crypto()),
    )
    if transient:
        async def unavailable(_current: Any) -> int:
            raise ConnectionError("synthetic temporary dependency outage")
        monkeypatch.setattr(runtime.service, "_run_resend", unavailable)
    async with env.engine.begin() as connection:
        if not transient:
            await connection.execute(text("UPDATE user_account SET status=0 WHERE id=:id"),
                                     {"id": env.confirmer.account_id})
        event_id = await connection.scalar(text(
            "SELECT id FROM outbox_event WHERE dedup_key=:key"
        ), {"key": f"uncertain.effect:{resolution}:1"})
        await connection.execute(text("""
            UPDATE outbox_event SET state='published',attempts=1,lease_id=:lease,
              lease_expires_at=now()+interval '60 seconds' WHERE id=:id
        """), {"id": event_id, "lease": uuid4()})
    try:
        with audit_principal_scope():
            if transient:
                with pytest.raises(ConnectionError):
                    await tasks._apply_uncertain_effect(resolution, str(event_id))
            else:
                assert await tasks._apply_uncertain_effect(resolution, str(event_id)) == 0
                assert await tasks._apply_uncertain_effect(resolution, str(event_id)) == 0
        async with env.engine.connect() as connection:
            event = (await connection.execute(text(
                "SELECT state,attempts FROM outbox_event WHERE id=:id"
            ), {"id": event_id})).mappings().one()
            state = await connection.scalar(text(
                "SELECT state FROM sms_uncertain_resolution WHERE id=:id"
            ), {"id": resolution})
        assert event["state"] == ("pending" if transient else "completed")
        assert event["attempts"] == 1
        assert state == ("retryable_effect_error" if transient else "manual_intervention_required")
    finally:
        await runtime.engine.dispose()
