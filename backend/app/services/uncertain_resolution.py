"""uncertain 保守终态后的双人处置；禁止把旧分片改回 pending。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import text

from app.core.auth.accounts import ActorPrincipal, SecurityPrincipal
from app.core.runtime_resources import database_engine
from app.services.crypto import CryptoService, EncryptionContext
from app.services.pipeline import SendRequest
from app.services.usage_ledger import request_usage_release_for_batch
from app.settings import Settings, get_settings

ResolutionAction = Literal[
    "confirm_accepted",
    "confirm_not_accepted",
    "keep_unknown",
    "resend_new_batch",
]
RESOLUTION_ACTIONS = frozenset(
    {
        "confirm_accepted",
        "confirm_not_accepted",
        "keep_unknown",
        "resend_new_batch",
    }
)
RESEND_UNKNOWN_BIZ_ID = "unknown-recipients-v1"


class UncertainResolutionConflict(RuntimeError):
    """处置状态不允许该操作。"""


class UncertainResolutionNotFound(LookupError):
    """处置单或分片不存在。"""


@dataclass(frozen=True, slots=True)
class UncertainResolution:
    id: int
    chunk_id: int
    batch_id: int
    action: str
    state: str
    proposer_account_id: int
    confirmer_account_id: int | None
    child_batch_id: int | None


class UncertainResolutionService:
    def __init__(
        self,
        crypto: CryptoService,
        settings: Settings | None = None,
    ) -> None:
        self.crypto = crypto
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def propose(
        self,
        chunk_id: int,
        action: str,
        principal: SecurityPrincipal,
    ) -> UncertainResolution:
        if action not in RESOLUTION_ACTIONS:
            raise ValueError("invalid uncertain resolution action")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                chunk = (
                    await connection.execute(
                        text(
                            """
                            SELECT c.id,c.batch_id
                            FROM sms_chunk c
                            WHERE c.id=:chunk_id AND c.status='unknown_terminal'
                            FOR UPDATE
                            """
                        ),
                        {"chunk_id": chunk_id},
                    )
                ).mappings().one_or_none()
                if chunk is None:
                    raise UncertainResolutionNotFound
                inserted = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_uncertain_resolution(
                              chunk_id,batch_id,action,proposer_account_id
                            ) VALUES (:chunk_id,:batch_id,:action,:account_id)
                            ON CONFLICT (chunk_id) DO NOTHING
                            RETURNING id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,child_batch_id
                            """
                        ),
                        {
                            "chunk_id": chunk_id,
                            "batch_id": int(chunk["batch_id"]),
                            "action": action,
                            "account_id": principal.account_id,
                        },
                    )
                ).mappings().one_or_none()
                if inserted is None:
                    raise UncertainResolutionConflict("该分片已有处置单")
                return _row(inserted)
        finally:
            await engine.dispose()

    async def confirm(
        self,
        resolution_id: int,
        principal: SecurityPrincipal,
        *,
        actor: ActorPrincipal | None = None,
    ) -> tuple[UncertainResolution, SendRequest | None]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                current = (
                    await connection.execute(
                        text(
                            """
                            SELECT id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,child_batch_id
                            FROM sms_uncertain_resolution
                            WHERE id=:id FOR UPDATE
                            """
                        ),
                        {"id": resolution_id},
                    )
                ).mappings().one_or_none()
                if current is None:
                    raise UncertainResolutionNotFound
                if str(current["state"]) != "proposed":
                    raise UncertainResolutionConflict("处置单已确认")
                if int(current["proposer_account_id"]) == principal.account_id:
                    raise UncertainResolutionConflict("确认人不能是提案人")
                updated = (
                    await connection.execute(
                        text(
                            """
                            UPDATE sms_uncertain_resolution
                            SET state='confirmed',
                                confirmer_account_id=:account_id,
                                confirmed_at=now()
                            WHERE id=:id AND state='proposed'
                            RETURNING id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,child_batch_id
                            """
                        ),
                        {"id": resolution_id, "account_id": principal.account_id},
                    )
                ).mappings().one_or_none()
                if updated is None:
                    raise UncertainResolutionConflict("处置单已确认")
                action = str(updated["action"])
                batch_id = int(updated["batch_id"])
                if action == "confirm_not_accepted":
                    unused = await connection.execute(
                        text(
                            """
                            SELECT NOT EXISTS (
                              SELECT 1 FROM sms_message
                              WHERE batch_id=:batch_id
                                AND status IN ('sent','delivered')
                            )
                            AND NOT EXISTS (
                              SELECT 1 FROM sms_chunk
                              WHERE batch_id=:batch_id
                                AND status IN ('submitted','submitting')
                            )
                            """
                        ),
                        {"batch_id": batch_id},
                    )
                    if bool(unused.scalar_one()):
                        await request_usage_release_for_batch(
                            connection,
                            batch_id=batch_id,
                            event_id=f"uncertain-unused:{updated['id']}",
                        )
                resend_request: SendRequest | None = None
                if action == "resend_new_batch":
                    resend_request = await self._build_resend(
                        connection,
                        chunk_id=int(updated["chunk_id"]),
                        actor=actor,
                    )
                return _row(updated), resend_request
        finally:
            await engine.dispose()

    async def attach_child_batch(self, resolution_id: int, child_batch_id: int) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE sms_uncertain_resolution
                        SET child_batch_id=:child_batch_id
                        WHERE id=:id AND action='resend_new_batch'
                          AND state='confirmed' AND child_batch_id IS NULL
                        """
                    ),
                    {"id": resolution_id, "child_batch_id": child_batch_id},
                )
        finally:
            await engine.dispose()

    async def _build_resend(
        self,
        connection: Any,
        *,
        chunk_id: int,
        actor: ActorPrincipal | None,
    ) -> SendRequest:
        batch = (
            await connection.execute(
                text(
                    """
                    SELECT trim(b.batch_no) batch_no,b.category,b.channel,b.dept,
                           b.send_content_enc,b.sign_name,b.consent_confirmed,b.is_test
                    FROM sms_chunk c
                    JOIN sms_batch b ON b.id=c.batch_id
                    WHERE c.id=:chunk_id AND c.status='unknown_terminal'
                    """
                ),
                {"chunk_id": chunk_id},
            )
        ).mappings().one()
        phones = (
            await connection.execute(
                text(
                    """
                    SELECT phone_enc,trim(phone_hmac) phone_hmac,key_version
                    FROM sms_message
                    WHERE chunk_id=:chunk_id AND status='unknown'
                    ORDER BY id
                    """
                ),
                {"chunk_id": chunk_id},
            )
        ).mappings()
        mobiles = tuple(
            self.crypto.decrypt_phone(
                bytes(item["phone_enc"]),
                int(item["key_version"]),
                str(item["phone_hmac"]),
            )
            for item in phones
        )
        if not mobiles:
            raise UncertainResolutionConflict("没有可重发的未知号码")
        content = self.crypto.decrypt_bound_packed_text(
            bytes(batch["send_content_enc"]),
            EncryptionContext(
                domain="sms-content",
                table="sms_batch",
                column="send_content_enc",
                object_id=str(batch["batch_no"]),
            ),
        )
        return SendRequest(
            category=str(batch["category"]),
            mobiles=mobiles,
            content=content,
            sign_name=str(batch["sign_name"]) if batch["sign_name"] is not None else None,
            channel=str(batch["channel"]),
            consent_confirmed=bool(batch["consent_confirmed"]),
            actor=actor,
            biz_id=f"{RESEND_UNKNOWN_BIZ_ID}:{uuid4().hex[:8]}",
            is_test=bool(batch["is_test"]),
            resend_of=str(batch["batch_no"]),
            resend_dept=str(batch["dept"]),
        )


def _row(row: Any) -> UncertainResolution:
    return UncertainResolution(
        int(row["id"]),
        int(row["chunk_id"]),
        int(row["batch_id"]),
        str(row["action"]),
        str(row["state"]),
        int(row["proposer_account_id"]),
        int(row["confirmer_account_id"]) if row["confirmer_account_id"] is not None else None,
        int(row["child_batch_id"]) if row["child_batch_id"] is not None else None,
    )
