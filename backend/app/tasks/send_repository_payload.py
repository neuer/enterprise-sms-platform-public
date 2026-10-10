"""发送分片的准备与受控解密载荷：按批次切分片、装配调用载荷。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.crypto import CryptoService, EncryptionContext
from app.services.vendor_test_recipient_repository import (
    lock_vendor_test_recipient_maintenance,
)
from app.settings import Settings
from app.tasks.send import (
    ChunkPayload,
)


class ChunkPayloadMixin:
    """SqlChunkStore 的分片准备与载荷装配；号码只在内存中受控解密。"""

    crypto: CryptoService
    settings: Settings

    if TYPE_CHECKING:
        # 由 SqlChunkStore 提供；运行时不定义桩，方法解析仍走 SqlChunkStore 的实现。
        def _engine(self) -> Any: ...

        @staticmethod
        def lane_for(category: str) -> str: ...

        @staticmethod
        async def _enqueue_chunk_ready(
            connection: AsyncConnection,
            chunk_ids: list[int],
            lane: str,
        ) -> None: ...

    async def _payload(
        self,
        connection: AsyncConnection,
        chunk_id: int,
    ) -> ChunkPayload:
        enforce_recipient_guard = bool(getattr(self.settings, "vendor_live_test", False))
        if enforce_recipient_guard:
            await lock_vendor_test_recipient_maintenance(connection)
        result = await connection.execute(
            text(
                """
                SELECT c.id chunk_id, c.batch_id, c.custom_id,c.retry_count,
                       c.status chunk_status,
                       COALESCE(c.selected_vendor,'zhihui') selected_vendor,
                       COALESCE(c.route_generation,1) route_generation,
                       c.next_vendor, COALESCE(c.route_policy_version,1) route_policy_version,
                       c.failover_from_attempt_id,
                       trim(b.batch_no) batch_no,b.send_content_enc,b.sign_name,
                       b.category,b.is_test, t.vendor_template_id
                FROM sms_chunk c
                JOIN sms_batch b ON b.id=c.batch_id
                LEFT JOIN sms_template t ON t.id=b.template_id
                WHERE c.id=:chunk_id
                """
            ),
            {"chunk_id": chunk_id},
        )
        row = result.mappings().one()
        phones_result = await connection.execute(
            text(
                """
                SELECT m.phone_enc, trim(m.phone_hmac) phone_hmac, m.key_version
                FROM sms_message m
                WHERE m.chunk_id=:chunk_id ORDER BY m.id, m.created_at
                """
            ),
            {"chunk_id": chunk_id},
        )
        phone_rows = list(phones_result.mappings())
        decrypted_phones = tuple(
            self.crypto.decrypt_phone(
                bytes(item["phone_enc"]),
                int(item["key_version"]),
                str(item["phone_hmac"]),
            )
            for item in phone_rows
        )
        denied_recipient_count = 0
        if enforce_recipient_guard:
            for phone in decrypted_phones:
                candidates = self.crypto.hmac_candidates(phone)
                conditions: list[str] = []
                parameters: dict[str, object] = {}
                for index, (version, digest) in enumerate(candidates.items()):
                    conditions.append(
                        f"(key_version=:version_{index} AND phone_hmac=:hmac_{index})"
                    )
                    parameters[f"version_{index}"] = version
                    parameters[f"hmac_{index}"] = digest
                allowed = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM vendor_test_recipient "
                        "WHERE status='active' AND (" + " OR ".join(conditions) + "))"
                    ),
                    parameters,
                )
                if not bool(allowed.scalar_one()):
                    denied_recipient_count += 1
        phones = () if denied_recipient_count else decrypted_phones
        return ChunkPayload(
            chunk_id=int(row["chunk_id"]),
            batch_id=int(row["batch_id"]),
            custom_id=str(row["custom_id"]).strip(),
            phones=phones,
            content=self.crypto.decrypt_bound_packed_text(
                bytes(row["send_content_enc"]),
                EncryptionContext(
                    domain="sms-content",
                    table="sms_batch",
                    column="send_content_enc",
                    object_id=str(row["batch_no"]),
                ),
            ),
            template_id=(
                str(row["vendor_template_id"]) if row["vendor_template_id"] is not None else ""
            ),
            sign_name=str(row["sign_name"] or ""),
            retry_count=int(row["retry_count"]),
            denied_recipient_count=denied_recipient_count,
            selected_vendor=str(row.get("selected_vendor") or "zhihui"),
            route_generation=max(1, int(row.get("route_generation") or 1)),
            category=str(row.get("category") or "notice"),
            is_test=bool(row["is_test"]),
            status=str(row.get("chunk_status") or "pending"),
            next_vendor=(str(row["next_vendor"]) if row.get("next_vendor") is not None else None),
            route_policy_version=max(1, int(row.get("route_policy_version") or 1)),
            failover_from_attempt_id=(
                int(row["failover_from_attempt_id"])
                if row.get("failover_from_attempt_id") is not None
                else None
            ),
        )

    async def refresh_invoke_payload(self, chunk_id: int) -> ChunkPayload:
        """令牌等待后重读真实联调收件人资格，不能复用旧 denied_count。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                return await self._payload(connection, chunk_id)
        finally:
            await engine.dispose()

    async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                status_result = await connection.execute(
                    text(
                        """
                        SELECT c.status, b.category, b.status batch_status
                        FROM sms_chunk c
                        JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.id=:chunk_id
                          AND b.status IN ('queued','sending')
                          AND (c.status='pending' OR (
                            c.status IN ('retrying','failover_pending')
                            AND (c.retry_not_before IS NULL OR c.retry_not_before<=now())
                          ))
                        """
                    ),
                    {"chunk_id": chunk_id},
                )
                row = status_result.mappings().one_or_none()
                if (
                    row is None
                    or row["status"] not in {"pending", "retrying", "failover_pending"}
                    or row["batch_status"] not in {"queued", "sending"}
                ):
                    return None
                return (
                    await self._payload(connection, chunk_id),
                    self.lane_for(str(row["category"])),
                )
        finally:
            await engine.dispose()

    async def prepare_chunks(
        self,
        batch_no: str,
        batch_size: int,
    ) -> tuple[list[int], str]:
        """规划分片并登记 child Outbox；事务内只处理元数据，不解密手机号。"""

        if batch_size < 1:
            raise ValueError("vendor_batch_size must be positive")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                batch_result = await connection.execute(
                    text(
                        "SELECT id, category, status, app_id, "
                        "usage_reservation_id, segments FROM sms_batch "
                        "WHERE batch_no=:batch_no "
                        "AND status IN ('queued','sending') FOR UPDATE"
                    ),
                    {"batch_no": batch_no},
                )
                batch = batch_result.mappings().one_or_none()
                if batch is None or str(batch["status"]) not in {"queued", "sending"}:
                    raise RuntimeError("批次状态不允许发送")
                batch_id = int(batch["id"])
                existing = await connection.execute(
                    text(
                        """
                        SELECT id FROM sms_chunk WHERE batch_id=:batch_id
                          AND (status='pending' OR (
                            status IN ('retrying','failover_pending')
                            AND (retry_not_before IS NULL OR retry_not_before<=now())
                          )) ORDER BY chunk_no
                        """
                    ),
                    {"batch_id": batch_id},
                )
                chunk_ids = [int(value) for value in existing.scalars()]
                if not chunk_ids:
                    any_chunks = await connection.execute(
                        text("SELECT count(*) FROM sms_chunk WHERE batch_id=:batch_id"),
                        {"batch_id": batch_id},
                    )
                    if int(any_chunks.scalar_one()) > 0:
                        if str(batch["status"]) == "queued":
                            await connection.execute(
                                text(
                                    "UPDATE sms_batch SET status='sending',updated_at=now() "
                                    "WHERE id=:id AND status='queued'"
                                ),
                                {"id": batch_id},
                            )
                        return [], self.lane_for(str(batch["category"]))
                    messages = await connection.execute(
                        text(
                            """
                            SELECT id, created_at FROM sms_message
                            WHERE batch_id=:batch_id AND chunk_id IS NULL
                            ORDER BY id, created_at
                            """
                        ),
                        {"batch_id": batch_id},
                    )
                    message_rows = list(messages.mappings())
                    for offset in range(0, len(message_rows), batch_size):
                        group = message_rows[offset : offset + batch_size]
                        chunk_no = offset // batch_size + 1
                        custom_id = f"{batch_no[:24]}{chunk_no:08d}"
                        inserted = await connection.execute(
                            text(
                                """
                                INSERT INTO sms_chunk (
                                  batch_id, chunk_no, custom_id, phone_count
                                ) VALUES (:batch_id,:chunk_no,:custom_id,:phone_count)
                                RETURNING id
                                """
                            ),
                            {
                                "batch_id": batch_id,
                                "chunk_no": chunk_no,
                                "custom_id": custom_id,
                                "phone_count": len(group),
                            },
                        )
                        chunk_id = int(inserted.scalar_one())
                        chunk_ids.append(chunk_id)
                        segment_count = int(batch["segments"] or 0) * len(group)
                        await connection.execute(
                            text(
                                """
                                INSERT INTO usage_chunk_allocation (
                                  chunk_id, batch_id, reservation_id,
                                  recipient_count, segment_count,
                                  request_count, app_id
                                ) VALUES (
                                  :chunk_id, :batch_id, :reservation_id,
                                  :recipients, :segments,
                                  :requests, :app_id
                                )
                                ON CONFLICT (chunk_id) DO NOTHING
                                """
                            ),
                            {
                                "chunk_id": chunk_id,
                                "batch_id": batch_id,
                                "reservation_id": batch["usage_reservation_id"],
                                "recipients": len(group),
                                "segments": segment_count,
                                "requests": 1 if chunk_no == 1 else 0,
                                "app_id": batch["app_id"],
                            },
                        )
                        await connection.execute(
                            text(
                                """
                                UPDATE sms_message SET chunk_id=:chunk_id
                                WHERE id=:id AND created_at=:created_at
                                """
                            ),
                            [
                                {
                                    "chunk_id": chunk_id,
                                    "id": item["id"],
                                    "created_at": item["created_at"],
                                }
                                for item in group
                            ],
                        )
                    await connection.execute(
                        text("UPDATE sms_batch SET status='sending',updated_at=now() WHERE id=:id"),
                        {"id": batch_id},
                    )
                    from app.services.send_inflight import materialize_in_flight_reservation

                    actual_chunks = int(
                        (
                            await connection.execute(
                                text("SELECT count(*) FROM sms_chunk WHERE batch_id=:batch_id"),
                                {"batch_id": batch_id},
                            )
                        ).scalar_one()
                    )
                    app_limit = 200
                    if batch["app_id"] is not None:
                        limit_row = await connection.execute(
                            text("SELECT max_in_flight_chunks FROM app WHERE id=:app_id"),
                            {"app_id": batch["app_id"]},
                        )
                        loaded_limit = limit_row.scalar_one_or_none()
                        if loaded_limit is not None:
                            app_limit = max(1, int(loaded_limit))
                    await materialize_in_flight_reservation(
                        connection,
                        batch_id=batch_id,
                        actual_chunks=actual_chunks,
                        limit=app_limit,
                    )
                elif str(batch["status"]) == "queued":
                    await connection.execute(
                        text(
                            "UPDATE sms_batch SET status='sending',updated_at=now() "
                            "WHERE id=:id AND status='queued'"
                        ),
                        {"id": batch_id},
                    )
                lane = self.lane_for(str(batch["category"]))
                await self._enqueue_chunk_ready(connection, chunk_ids, lane)
                return chunk_ids, lane
        finally:
            await engine.dispose()
