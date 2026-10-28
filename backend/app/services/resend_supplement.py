"""显式补发未覆盖的失败号码。不复用普通重发的固定幂等键。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.accounts import ActorPrincipal
from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.services.batch_query import BatchAccessScope, BatchNotFound
from app.services.crypto import CryptoService, EncryptionContext
from app.services.pipeline_contracts import FailedSourceReference, SendRequest
from app.services.resend_eligibility import ResendBlocked, read_coverage
from app.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class SupplementPreview:
    intent_id: UUID
    covered_failed: int
    uncovered_failed: int
    child_batch_no: str | None
    phone_masks: tuple[str, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class SupplementSubmit:
    batch_no: str
    accepted: int
    status: str
    idempotent: bool


class ResendSupplementService:
    """预览冻结未覆盖消息，确认后只把该集合送进原发送流水线。"""

    def __init__(
        self,
        crypto: CryptoService,
        settings: Settings | None = None,
    ) -> None:
        self.crypto = crypto
        self.settings = settings or get_settings()

    def _engine(self) -> ManagedAsyncEngine:
        return database_engine(self.settings.database_url)

    async def preview(self, batch_no: str, scope: BatchAccessScope) -> SupplementPreview:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                return await self._preview(connection, batch_no, scope)
        finally:
            await engine.dispose()

    async def confirm(
        self,
        batch_no: str,
        intent_id: UUID,
        scope: BatchAccessScope,
        *,
        actor: ActorPrincipal | None,
        accept: Callable[[SendRequest], Awaitable[Any]],
    ) -> SupplementSubmit:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                prepared = await self._prepare_confirm(
                    connection, batch_no, intent_id, scope, actor
                )
            if isinstance(prepared, SupplementSubmit):
                return prepared
        finally:
            await engine.dispose()
        result = await accept(prepared)
        await self._mark_submitted(intent_id, str(result.batch_no))
        return SupplementSubmit(
            str(result.batch_no),
            int(result.accepted),
            str(result.status),
            bool(result.idempotent),
        )

    async def _preview(
        self,
        connection: AsyncConnection,
        batch_no: str,
        scope: BatchAccessScope,
    ) -> SupplementPreview:
        predicate, params = scope.sql()
        locked = await connection.execute(
            text(
                f"""
                SELECT b.id, b.status FROM sms_batch b
                WHERE b.batch_no=CAST(:batch_no AS char(32)) AND {predicate}
                FOR UPDATE
                """
            ),
            {"batch_no": batch_no, **params},
        )
        source = locked.mappings().one_or_none()
        if source is None:
            raise BatchNotFound
        coverage = await read_coverage(connection, batch_no, predicate, params)
        if coverage is None:
            raise BatchNotFound
        if coverage.block_reason != "supplement_required":
            raise ResendBlocked(
                coverage.block_reason or "nothing_uncovered",
                covered=coverage.covered_failed,
                uncovered=coverage.uncovered_failed,
                child_batch_no=coverage.child_batch_no,
            )
        existing = await connection.execute(
            text(
                """
                SELECT id FROM sms_resend_supplement
                WHERE source_batch_id=:batch_id AND state='preview'
                FOR UPDATE
                """
            ),
            {"batch_id": source["id"]},
        )
        current = existing.mappings().one_or_none()
        intent_id = UUID(str(current["id"])) if current is not None else uuid4()
        created = current is None
        if created:
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_resend_supplement(
                      id, source_batch_id, state
                    ) VALUES (:id, :batch_id, 'preview')
                    """
                ),
                {"id": intent_id, "batch_id": source["id"]},
            )
        await connection.execute(
            text(
                """
                UPDATE sms_resend_supplement_member mem
                SET supplement_id=:intent_id, claim_state='frozen', child_batch_id=NULL
                FROM sms_message m
                WHERE mem.source_message_id=m.id
                  AND mem.source_message_created_at=m.created_at
                  AND m.batch_id=:batch_id AND m.status='failed'
                  AND mem.claim_state='released'
                """
            ),
            {"intent_id": intent_id, "batch_id": source["id"]},
        )
        await connection.execute(
            text(
                """
                INSERT INTO sms_resend_supplement_member(
                  source_batch_id, source_message_id, source_message_created_at,
                  supplement_id, generation, claim_state
                )
                SELECT m.batch_id, m.id, m.created_at, :intent_id, 1, 'frozen'
                FROM sms_message m
                WHERE m.batch_id=:batch_id AND m.status='failed'
                  AND NOT EXISTS (
                    SELECT 1 FROM sms_resend_supplement_member mem
                    WHERE mem.source_batch_id=m.batch_id
                      AND mem.source_message_id=m.id
                      AND mem.source_message_created_at=m.created_at
                      AND mem.claim_state IN ('frozen','submitted')
                  )
                ON CONFLICT DO NOTHING
                """
            ),
            {"intent_id": intent_id, "batch_id": source["id"]},
        )
        masks = await connection.execute(
            text(
                """
                SELECT m.phone_mask
                FROM sms_resend_supplement_member mem
                JOIN sms_message m
                  ON m.id=mem.source_message_id
                 AND m.created_at=mem.source_message_created_at
                WHERE mem.supplement_id=:intent_id AND mem.claim_state='frozen'
                ORDER BY m.id
                """
            ),
            {"intent_id": intent_id},
        )
        phone_masks = tuple(str(row["phone_mask"]) for row in masks.mappings())
        return SupplementPreview(
            intent_id,
            coverage.covered_failed,
            len(phone_masks),
            coverage.child_batch_no,
            phone_masks,
            created,
        )

    async def _prepare_confirm(
        self,
        connection: AsyncConnection,
        batch_no: str,
        intent_id: UUID,
        scope: BatchAccessScope,
        actor: ActorPrincipal | None,
    ) -> SendRequest | SupplementSubmit:
        predicate, params = scope.sql()
        locked_batch = await connection.execute(
            text(
                f"""
                SELECT b.id, b.batch_no, b.category, b.channel, b.dept,
                  b.send_content_enc, b.sign_name, b.consent_confirmed, b.is_test
                FROM sms_batch b
                WHERE b.batch_no=CAST(:batch_no AS char(32)) AND {predicate}
                FOR UPDATE OF b
                """
            ),
            {"batch_no": batch_no, **params},
        )
        source = locked_batch.mappings().one_or_none()
        if source is None:
            raise BatchNotFound
        intent = await connection.execute(
            text(
                """
                SELECT id, state, child_batch_id
                FROM sms_resend_supplement
                WHERE id=:id AND source_batch_id=:batch_id
                FOR UPDATE
                """
            ),
            {"id": intent_id, "batch_id": source["id"]},
        )
        row = intent.mappings().one_or_none()
        if row is None:
            raise BatchNotFound
        if row["state"] == "submitted" and row["child_batch_id"] is not None:
            child = await connection.execute(
                text(
                    """
                    SELECT trim(batch_no) AS batch_no, status, total
                    FROM sms_batch WHERE id=:id
                    """
                ),
                {"id": row["child_batch_id"]},
            )
            found = child.mappings().one()
            return SupplementSubmit(
                str(found["batch_no"]),
                int(found["total"]),
                str(found["status"]),
                True,
            )
        members = await connection.execute(
            text(
                """
                SELECT m.id, m.created_at, m.status, m.phone_enc,
                  trim(m.phone_hmac) AS phone_hmac, m.key_version
                FROM sms_resend_supplement_member mem
                JOIN sms_message m
                  ON m.id=mem.source_message_id
                 AND m.created_at=mem.source_message_created_at
                WHERE mem.supplement_id=:id AND mem.claim_state='frozen'
                ORDER BY m.id
                FOR UPDATE OF m
                """
            ),
            {"id": intent_id},
        )
        rows = list(members.mappings())
        if not rows or any(item["status"] != "failed" for item in rows):
            await connection.execute(
                text(
                    """
                    UPDATE sms_resend_supplement SET state='rejected'
                    WHERE id=:id AND state='preview'
                    """
                ),
                {"id": intent_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE sms_resend_supplement_member
                    SET claim_state='released'
                    WHERE supplement_id=:id AND claim_state='frozen'
                    """
                ),
                {"id": intent_id},
            )
            raise ResendBlocked("preview_stale")
        content = self.crypto.decrypt_bound_packed_text(
            bytes(source["send_content_enc"]),
            EncryptionContext(
                domain="sms-content",
                table="sms_batch",
                column="send_content_enc",
                object_id=str(source["batch_no"]).strip(),
            ),
        )
        mobiles = tuple(
            self.crypto.decrypt_phone(
                bytes(item["phone_enc"]),
                int(item["key_version"]),
                str(item["phone_hmac"]),
            )
            for item in rows
        )
        sign_name = source["sign_name"]
        return SendRequest(
            category=str(source["category"]),
            mobiles=mobiles,
            content=content,
            sign_name=None if sign_name is None else str(sign_name),
            channel=str(source["channel"]),
            consent_confirmed=bool(source["consent_confirmed"]),
            biz_id=intent_id.hex,
            is_test=bool(source["is_test"]),
            resend_of=str(source["batch_no"]).strip(),
            resend_dept=str(source["dept"]),
            actor=actor,
            failed_sources=tuple(
                FailedSourceReference(
                    int(item["id"]),
                    item["created_at"],
                    str(item["phone_hmac"]),
                    int(item["key_version"]),
                )
                for item in rows
            ),
        )

    async def _mark_submitted(self, intent_id: UUID, child_batch_no: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        WITH child AS (
                          SELECT id FROM sms_batch
                          WHERE batch_no=CAST(:batch_no AS char(32))
                        )
                        UPDATE sms_resend_supplement s
                        SET state='submitted', child_batch_id=child.id,
                          submitted_at=clock_timestamp()
                        FROM child
                        WHERE s.id=:id AND s.state='preview'
                        """
                    ),
                    {"id": intent_id, "batch_no": child_batch_no},
                )
                await connection.execute(
                    text(
                        """
                        UPDATE sms_resend_supplement_member mem
                        SET claim_state='submitted',
                          child_batch_id=s.child_batch_id
                        FROM sms_resend_supplement s
                        WHERE mem.supplement_id=s.id AND s.id=:id
                          AND mem.claim_state='frozen'
                        """
                    ),
                    {"id": intent_id},
                )
        finally:
            await engine.dispose()
