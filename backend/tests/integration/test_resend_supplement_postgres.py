"""补发完整事务的 PostgreSQL/Redis 回归：预览、并发、晚到回执及回滚。"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.apikey import ApiAppContext
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services.batch_query import BatchAccessScope
from app.services.crypto import EncryptionContext
from app.services.pipeline_contracts import SendRequest
from app.services.resend_eligibility import ResendBlocked, read_coverage
from app.services.resend_supplement import ResendSupplementService
from tests.integration import test_uncertain_web_resend_postgres as shared_fixture
from tests.integration.test_uncertain_web_resend_postgres import (
    _create_admin,
    _NoDisposeEngine,
)

resend_env = shared_fixture.resend_env

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis 7",
)


async def seed_legacy(env: Any, count: int = 2) -> tuple[Any, str, int, list[int]]:
    """构造升级前部分普通重发，号码仅存密文四列。"""
    actor = await _create_admin(env.engine, f"supp-{uuid4().hex[:8]}")
    batch_no, ordinary_no = uuid4().hex, uuid4().hex
    async with env.engine.begin() as c:

        async def batch(no: str, total: int, source: int | None = None) -> int:
            return int(
                await c.scalar(
                    text("""
                INSERT INTO sms_batch(batch_no,channel,dept,creator,creator_account_id,
                  creator_identity_id,display_content_enc,send_content_enc,status,total,
                  failed,resend_of)
                VALUES(:no,'web','平台部',:creator,:account,:identity,:display,:send,
                       'completed',:total,:total,:source) RETURNING id
            """),
                    {
                        "no": no,
                        "creator": actor.actor_name,
                        "account": actor.account_id,
                        "identity": actor.identity_id,
                        "total": total,
                        "source": source,
                        "display": env.crypto.encrypt_bound_packed_text(
                            "补发事务验收",
                            EncryptionContext(
                                domain="sms-display-content",
                                table="sms_batch",
                                column="display_content_enc",
                                object_id=no,
                            ),
                        ),
                        "send": env.crypto.encrypt_bound_packed_text(
                            "补发事务验收",
                            EncryptionContext(
                                domain="sms-content",
                                table="sms_batch",
                                column="send_content_enc",
                                object_id=no,
                            ),
                        ),
                    },
                )
            )

        source = await batch(batch_no, count)
        ordinary = await batch(ordinary_no, 1, source)
        await c.execute(
            text("INSERT INTO sms_resend_action(source_batch_id,child_batch_id) VALUES(:s,:c)"),
            {"s": source, "c": ordinary},
        )
        ids = []
        for i in range(count):
            protected = env.crypto.protect_phone(f"17{int(uuid4().hex[:6], 16):08d}{i}")
            row = (
                (
                    await c.execute(
                        text("""
                INSERT INTO sms_message(batch_id,phone_enc,phone_hmac,phone_mask,key_version,status)
                VALUES(:b,:enc,:hmac,:mask,:version,'failed') RETURNING id,created_at
            """),
                        {
                            "b": source,
                            "enc": protected.phone_enc,
                            "hmac": protected.phone_hmac,
                            "mask": protected.phone_mask,
                            "version": protected.key_version,
                        },
                    )
                )
                .mappings()
                .one()
            )
            ids.append(int(row["id"]))
            if i == 0:
                await c.execute(
                    text("""
                    INSERT INTO sms_resend_supplement_member(source_batch_id,source_message_id,
                      source_message_created_at,child_batch_id,generation,claim_state)
                    VALUES(:s,:m,:created,:c,1,'submitted')
                """),
                    {"s": source, "m": row["id"], "created": row["created_at"], "c": ordinary},
                )
    return actor, batch_no, source, ids


def service(env: Any) -> ResendSupplementService:
    result = ResendSupplementService(env.crypto, env.settings)
    result._engine = lambda: _NoDisposeEngine(env.engine)  # type: ignore[method-assign]
    return result


@pytest.mark.asyncio
async def test_cancel_reopen_concurrent_confirm_creates_one_distinct_child(resend_env: Any) -> None:
    env = resend_env
    actor, batch, source, _ids = await seed_legacy(env)
    other = await _create_admin(env.engine, f"other-{uuid4().hex[:8]}")
    subject = service(env)
    scope = BatchAccessScope(dept="平台部")
    first = await subject.preview(batch, scope)
    again = await subject.preview(batch, scope)
    assert again.intent_id == first.intent_id and not again.created
    assert (first.covered_failed, first.uncovered_failed) == (1, 1)
    assert (again.covered_failed, again.uncovered_failed) == (1, 1)
    async with env.engine.connect() as c:
        coverage = await read_coverage(c, batch, *scope.sql())
        assert coverage is not None and coverage.uncovered_failed == 1
    app = ApiAppContext(0, "web", "平台部", frozenset({"notice", "market"}), daily_quota=0)

    async def confirm(principal: Any) -> Any:
        async def accept(request: SendRequest) -> Any:
            return await env.pipeline.accept(app, request)

        with audit_principal_scope(principal), correlation_scope(uuid4()):
            return await subject.confirm(
                batch, first.intent_id, scope, actor=principal, accept=accept
            )

    results = await asyncio.gather(confirm(actor), confirm(other))
    assert results[0].batch_no == results[1].batch_no
    assert all(r.accepted == 1 for r in results)
    async with env.engine.connect() as c:
        facts = (
            (
                await c.execute(
                    text("""
            SELECT (SELECT count(*) FROM sms_batch WHERE resend_of=:s) children,
              (SELECT count(*) FROM sms_resend_action WHERE source_batch_id=:s) actions,
              (SELECT count(*) FROM sms_resend_supplement_member WHERE source_batch_id=:s
                AND claim_state='submitted') covered,
              (SELECT trim(batch_no) FROM sms_batch WHERE id=(SELECT child_batch_id
                FROM sms_resend_action WHERE source_batch_id=:s)) ordinary,
              (SELECT state FROM sms_resend_supplement WHERE id=:i) intent_state,
              (SELECT count(*) FROM outbox_event WHERE event_type='batch.ready'
                AND aggregate_id=:b) outbox
        """),
                    {"s": source, "i": first.intent_id, "b": results[0].batch_no},
                )
            )
            .mappings()
            .one()
        )
    assert facts["children"] == 2 and facts["actions"] == 1 and facts["covered"] == 2
    assert facts["ordinary"] != results[0].batch_no and facts["intent_state"] == "submitted"
    assert facts["outbox"] == 1
    replay = await confirm(other)
    assert replay.idempotent and replay.batch_no == results[0].batch_no


@pytest.mark.asyncio
async def test_late_delivery_releases_stale_preview_and_commits_rejection(resend_env: Any) -> None:
    env = resend_env
    actor, batch, source, ids = await seed_legacy(env, 3)
    subject = service(env)
    scope = BatchAccessScope(dept="平台部")
    preview = await subject.preview(batch, scope)
    assert preview.uncovered_failed == 2
    async with env.engine.begin() as c:
        await c.execute(
            text("UPDATE sms_message SET status='delivered' WHERE id=:id"), {"id": ids[1]}
        )

    async def forbidden(_request: SendRequest) -> Any:
        raise AssertionError("过期确认不能进入发送流水线")

    with pytest.raises(ResendBlocked) as blocked:
        await subject.confirm(batch, preview.intent_id, scope, actor=actor, accept=forbidden)
    assert blocked.value.reason == "preview_stale"
    async with env.engine.connect() as c:
        assert (
            await c.scalar(
                text("SELECT state FROM sms_resend_supplement WHERE id=:id"),
                {"id": preview.intent_id},
            )
            == "rejected"
        )
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM sms_resend_supplement_member "
                    "WHERE supplement_id=:id AND claim_state='released'"
                ),
                {"id": preview.intent_id},
            )
            == 2
        )
    replacement = await subject.preview(batch, scope)
    assert replacement.intent_id != preview.intent_id and replacement.uncovered_failed == 1
    with pytest.raises(ResendBlocked):
        await subject.confirm(batch, preview.intent_id, scope, actor=actor, accept=forbidden)


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_child_intent_and_coverage(
    resend_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """故障发生在完整子批次插入之后，确认覆盖不能先于发送事实提交。"""
    import app.services.pipeline_repository as repository

    env = resend_env
    actor, batch, source, _ids = await seed_legacy(env)
    subject = service(env)
    scope = BatchAccessScope(dept="平台部")
    preview = await subject.preview(batch, scope)
    original = repository.enqueue_outbox

    async def fail_outbox(_connection: Any, _spec: Any) -> None:
        raise RuntimeError("synthetic outbox transaction failure")

    async def accept(request: SendRequest) -> Any:
        return await env.pipeline.accept(
            ApiAppContext(0, "web", "平台部", frozenset({"notice"}), daily_quota=0),
            request,
        )

    monkeypatch.setattr(repository, "enqueue_outbox", fail_outbox)
    with audit_principal_scope(actor), correlation_scope(uuid4()), pytest.raises(RuntimeError):
        await subject.confirm(batch, preview.intent_id, scope, actor=actor, accept=accept)
    async with env.engine.connect() as c:
        assert (
            await c.scalar(text("SELECT count(*) FROM sms_batch WHERE resend_of=:s"), {"s": source})
            == 1
        )
        assert (
            await c.scalar(
                text("SELECT state FROM sms_resend_supplement WHERE id=:i"),
                {"i": preview.intent_id},
            )
            == "preview"
        )
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM sms_resend_supplement_member "
                    "WHERE supplement_id=:i AND claim_state='frozen'"
                ),
                {"i": preview.intent_id},
            )
            == 1
        )
    monkeypatch.setattr(repository, "enqueue_outbox", original)
    with audit_principal_scope(actor), correlation_scope(uuid4()):
        restored = await subject.confirm(
            batch, preview.intent_id, scope, actor=actor, accept=accept
        )
    assert restored.accepted == 1


@pytest.mark.asyncio
async def test_existing_preview_never_silently_expands_its_frozen_audience(resend_env: Any) -> None:
    """新失败不加入已经向操作者展示的旧意图，需再次明确预览和确认。"""
    env = resend_env
    _actor, batch, _source, ids = await seed_legacy(env, 3)
    subject = service(env)
    scope = BatchAccessScope(dept="平台部")
    async with env.engine.begin() as c:
        await c.execute(
            text("UPDATE sms_message SET status='unknown' WHERE id=:id"), {"id": ids[2]}
        )
    first = await subject.preview(batch, scope)
    assert first.uncovered_failed == 1
    async with env.engine.begin() as c:
        await c.execute(text("UPDATE sms_message SET status='failed' WHERE id=:id"), {"id": ids[2]})
    again = await subject.preview(batch, scope)
    assert again.intent_id == first.intent_id and again.phone_masks == first.phone_masks
    assert again.uncovered_failed == 1
