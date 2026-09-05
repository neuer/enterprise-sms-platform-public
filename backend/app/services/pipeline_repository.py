"""发送受理流水线的 PostgreSQL 事实源与模板读取。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.accounts import SecurityPrincipal, UncertainEffectPrincipal
from app.core.runtime_resources import database_engine, redis_client
from app.services.approval_repository import record_pending_approval_alert
from app.services.blacklist import RedisBlacklistCache
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.category import queue_for_category
from app.services.content_protection import decrypt_template_content
from app.services.crypto import CryptoService
from app.services.idempotency import IdempotencyFingerprint, IdempotencyScope
from app.services.import_repository import consume_import_reservation
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.pipeline import BatchCommand, BatchResponse, StoredBatch
from app.services.sensitive import SENSITIVE_WORD_REVISION_KEY, sensitive_word_index
from app.services.template import render_template
from app.services.usage_ledger import commit_usage_reservation, shanghai_day
from app.settings import Settings, get_settings

IDEMPOTENCY_LIVE_SQL = """
(
  i.expires_at > now()
  OR b.status IN (
    'pending_approval','scheduled','queued','sending','balance_blocked'
  )
  OR EXISTS (
    SELECT 1 FROM sms_chunk c
    WHERE c.batch_id=b.id
      AND c.status IN (
        'uncertain','unknown_terminal','submitting','retrying','pending',
        'split_capacity_blocked'
      )
  )
  OR EXISTS (
    SELECT 1 FROM callback_task t
    WHERE t.batch_id=b.id AND t.status IN ('pending','retrying')
  )
)
"""


class SqlPipelineStore:
    """在同一事务创建批次、消息三列、幂等记录与无 PII 审计。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load_config(self, dept: str) -> dict[str, Any]:
        async with self._engine().connect() as connection:
            config_result = await connection.execute(text("SELECT key, value FROM sys_config"))
            config = {str(row["key"]): str(row["value"]) for row in config_result.mappings()}
            quota_result = await connection.execute(
                text("SELECT daily_quota FROM dept_quota WHERE dept=:dept"),
                {"dept": dept},
            )
            config["dept_daily_quota"] = str(quota_result.scalar_one_or_none() or 0)
            return config

    async def read_dept_quota_usage(self, dept: str, *, now: datetime | None = None) -> int:
        """读取部门当日配额投影绝对值；usage_projection 与事实同事务一致，无行即零。

        只读路径，供发送预检展示配额摘要；发送入口的配额判定仍以
        usage_ledger 串行事实为准，二者不得互相替代。
        """

        date_key, _, _ = shanghai_day(now or datetime.now(UTC))
        async with self._engine().connect() as connection:
            value = await connection.scalar(
                text(
                    """
                    SELECT value FROM usage_projection
                    WHERE dimension_key=:key AND expires_at>now()
                    """
                ),
                {"key": f"quota:dept:{dept}:{date_key}"},
            )
            return int(value or 0)

    async def response_for(self, batch_no: str) -> BatchResponse:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                text(
                    """
                        SELECT b.batch_no, b.total, b.removed_duplicate,
                               b.removed_blacklist, b.removed_freq, b.segments,
                               b.quota_cost, b.status, b.deferred_reason,
                               b.scheduled_at, i.expires_at
                        FROM sms_batch b
                        LEFT JOIN idempotency_record i ON i.batch_id=b.id
                        WHERE b.batch_no=:batch_no
                        """
                ),
                {"batch_no": batch_no},
            )
            row = result.mappings().one()
            return BatchResponse(
                batch_no=str(row["batch_no"]).strip(),
                idempotent=True,
                accepted=int(row["total"]),
                removed_duplicate=int(row["removed_duplicate"]),
                removed_blacklist=int(row["removed_blacklist"]),
                removed_freq_limit=int(row["removed_freq"]),
                est_segments=int(row["segments"]),
                quota_cost=int(row["quota_cost"]),
                status=str(row["status"]),
                deferred_reason=(
                    str(row["deferred_reason"]) if row["deferred_reason"] is not None else None
                ),
                scheduled_at=cast(Any, row["scheduled_at"]),
                idempotency_expires_at=cast(Any, row.get("expires_at")),
            )

    async def count_in_flight_chunks(self, app_id: int) -> int:
        """统计该应用仍占用系统容量的分片：pending/submitting/retrying/uncertain。"""

        async with self._engine().connect() as connection:
            value = await connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM sms_chunk AS chunk
                    JOIN sms_batch AS batch ON batch.id = chunk.batch_id
                    WHERE batch.app_id = :app_id
                      AND chunk.status IN (
                        'pending', 'submitting', 'retrying', 'uncertain'
                      )
                    """
                ),
                {"app_id": app_id},
            )
            return int(value or 0)

    async def reserve_in_flight_chunks(
        self,
        app_id: int,
        estimated: int,
        limit: int,
    ) -> Any:
        if estimated < 1 or limit < 1:
            raise ValueError("in-flight reservation bounds invalid")
        from app.services.send_inflight import (
            InFlightInvariantViolation,
            conservation_error,
            mark_conservation_blocked,
            reserve_in_flight_chunks,
        )

        try:
            async with self._engine().begin() as connection:
                return await reserve_in_flight_chunks(
                    connection,
                    app_id=app_id,
                    estimated=estimated,
                    limit=limit,
                )
        except InFlightInvariantViolation:
            async with self._engine().begin() as connection:
                await mark_conservation_blocked(connection, app_id)
            raise
        except IntegrityError as exc:
            if not conservation_error(exc):
                raise
            async with self._engine().begin() as connection:
                await mark_conservation_blocked(connection, app_id)
            raise InFlightInvariantViolation("reserve") from exc

    async def release_in_flight_reservation(
        self,
        reservation_id: int,
        generation: int,
        reason: str,
        app_id: int | None = None,
    ) -> bool:
        from app.services.send_inflight import (
            InFlightInvariantViolation,
            conservation_error,
            mark_conservation_blocked,
            release_in_flight_reservation,
        )

        try:
            async with self._engine().begin() as connection:
                return await release_in_flight_reservation(
                    connection,
                    reservation_id=reservation_id,
                    generation=generation,
                    reason=reason,
                    app_id=app_id,
                )
        except (InFlightInvariantViolation, IntegrityError) as exc:
            if isinstance(exc, IntegrityError) and not conservation_error(exc):
                raise
            if app_id is not None:
                async with self._engine().begin() as connection:
                    await mark_conservation_blocked(connection, app_id)
            if isinstance(exc, InFlightInvariantViolation):
                raise
            raise InFlightInvariantViolation("release") from exc

    async def release_unbound_acceptance_reservation(
        self,
        reservation_id: int,
        generation: int,
        app_id: int,
    ) -> bool:
        from app.services.send_inflight import (
            InFlightInvariantViolation,
            conservation_error,
            mark_conservation_blocked,
            release_unbound_acceptance_reservation,
        )

        try:
            async with self._engine().begin() as connection:
                return await release_unbound_acceptance_reservation(
                    connection,
                    reservation_id=reservation_id,
                    generation=generation,
                    app_id=app_id,
                )
        except (InFlightInvariantViolation, IntegrityError) as exc:
            if isinstance(exc, IntegrityError) and not conservation_error(exc):
                raise
            async with self._engine().begin() as connection:
                await mark_conservation_blocked(connection, app_id)
            if isinstance(exc, InFlightInvariantViolation):
                raise
            raise InFlightInvariantViolation("release") from exc

    async def resolve_ambiguous_acceptance_commit(
        self,
        *,
        reservation_id: int,
        generation: int,
        app_id: int,
        scope_kind: str,
        scope_id: str,
        biz_id: str,
        request_hash: str,
    ) -> Any:
        from app.services.send_inflight import resolve_ambiguous_acceptance_commit

        try:
            async with self._engine().begin() as connection:
                return await resolve_ambiguous_acceptance_commit(
                    connection,
                    reservation_id=reservation_id,
                    generation=generation,
                    app_id=app_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    biz_id=biz_id,
                    request_hash=request_hash,
                )
        except Exception:
            from app.services.send_inflight import AcceptCommitResolution

            return AcceptCommitResolution("UNKNOWN")

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        if not phone_hmacs:
            return set()
        repository = SqlBlacklistRepository(self.settings)
        return await RedisBlacklistCache(redis_client(self.settings.redis_control_url)).matches(
            phone_hmacs,
            repository.all_hmacs,
        )

    async def sensitive_hits(self, content: str) -> list[str]:
        return await sensitive_word_index.match(
            content,
            self._sensitive_words,
            self._sensitive_word_revision,
        )

    async def _sensitive_word_revision(self) -> int:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                text("SELECT value FROM sys_config WHERE key=:key"),
                {"key": SENSITIVE_WORD_REVISION_KEY},
            )
            value = result.scalar_one_or_none()
            return int(value) if value is not None else 0

    async def _sensitive_words(self) -> list[str]:
        async with self._engine().connect() as connection:
            result = await connection.execute(text("SELECT word FROM sensitive_word"))
            return [str(value) for value in result.scalars()]

    async def audit_sensitive_hit(self, app_id: int, hit_count: int) -> None:
        async with self._engine().begin() as connection:
            await connection.execute(
                text(
                    """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_app_id,action,object_type,
                          object_id,after_val
                        )
                        VALUES(:actor,'api_app',:app_id,'sensitive_hit','app',:object_id,
                          jsonb_build_object('hit_count',:hit_count))
                        """
                ),
                {
                    "actor": f"app:{app_id}",
                    "app_id": app_id,
                    "object_id": str(app_id),
                    "hit_count": hit_count,
                },
            )

    async def reserve_idempotency_claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        token: str,
        fingerprint: str,
        ttl_s: int,
    ) -> int | None:
        """占用或接管可抢占 Claim；completed 不可抢占。"""

        async with self._engine().begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO idempotency_claim (
                          scope_kind, scope_id, biz_id, token, fingerprint,
                          generation, expires_at, state
                        ) VALUES (
                          :scope_kind, :scope_id, :biz_id, :token, :fingerprint,
                          1, now() + make_interval(secs => :ttl_s), 'active'
                        )
                        ON CONFLICT (scope_kind, scope_id, biz_id)
                        DO UPDATE SET
                          generation = idempotency_claim.generation + 1,
                          token = EXCLUDED.token,
                          fingerprint = EXCLUDED.fingerprint,
                          expires_at = EXCLUDED.expires_at,
                          state = 'active',
                          batch_id = NULL,
                          completed_at = NULL,
                          released_at = NULL,
                          release_reason = NULL,
                          updated_at = now()
                        WHERE idempotency_claim.state IN ('expired','released')
                           OR (
                             idempotency_claim.state='active'
                             AND idempotency_claim.expires_at < now()
                           )
                        RETURNING generation
                        """
                    ),
                    {
                        "scope_kind": scope.kind,
                        "scope_id": scope.id,
                        "biz_id": biz_id,
                        "token": token,
                        "fingerprint": fingerprint,
                        "ttl_s": ttl_s,
                    },
                )
            ).scalar_one_or_none()
        return int(row) if row is not None else None

    async def renew_idempotency_claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        token: str,
        fingerprint: str,
        generation: int,
        ttl_s: int,
        clock_margin_s: int = 1,
    ) -> bool:
        """权威续租；旧 generation/token 不得改写新 owner。"""

        async with self._engine().begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE idempotency_claim SET
                          expires_at=now()+make_interval(secs=>:ttl_s),
                          updated_at=now()
                        WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                          AND biz_id=:biz_id AND token=:token
                          AND fingerprint=:fingerprint
                          AND generation=:generation
                          AND state='active'
                          AND expires_at>now()-make_interval(secs=>:margin_s)
                        RETURNING generation
                        """
                    ),
                    {
                        "scope_kind": scope.kind,
                        "scope_id": scope.id,
                        "biz_id": biz_id,
                        "token": token,
                        "fingerprint": fingerprint,
                        "generation": generation,
                        "ttl_s": ttl_s,
                        "margin_s": clock_margin_s,
                    },
                )
            ).scalar_one_or_none()
        return row is not None

    async def release_idempotency_claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        token: str,
        generation: int,
        reason: str = "abandoned",
    ) -> bool:
        """仅释放仍 active 且未绑定批次的 Claim。"""

        async with self._engine().begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE idempotency_claim SET
                          state='released',
                          released_at=now(),
                          release_reason=:reason,
                          updated_at=now()
                        WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                          AND biz_id=:biz_id AND token=:token
                          AND generation=:generation
                          AND state='active'
                          AND batch_id IS NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM idempotency_record r
                            WHERE r.scope_kind=idempotency_claim.scope_kind
                              AND r.scope_id=idempotency_claim.scope_id
                              AND r.biz_id=idempotency_claim.biz_id
                              AND r.expires_at>now()
                          )
                        RETURNING id
                        """
                    ),
                    {
                        "scope_kind": scope.kind,
                        "scope_id": scope.id,
                        "biz_id": biz_id,
                        "token": token,
                        "generation": generation,
                        "reason": reason[:32],
                    },
                )
            ).scalar_one_or_none()
        return row is not None

    async def load_idempotency_claim(
        self, scope: IdempotencyScope, biz_id: str
    ) -> dict[str, Any] | None:
        """读取权威 Claim，供 Redis 缺失时重建或判断可抢占。"""

        async with self._engine().connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT token, fingerprint, generation, state, batch_id,
                               expires_at > now() AS lease_valid
                        FROM idempotency_claim
                        WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                          AND biz_id=:biz_id
                        """
                        ),
                        {
                            "scope_kind": scope.kind,
                            "scope_id": scope.id,
                            "biz_id": biz_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    async def live_idempotency_claim(self, scope: IdempotencyScope, biz_id: str) -> bool:
        async with self._engine().connect() as connection:
            exists = (
                await connection.execute(
                    text(
                        """
                        SELECT 1 FROM idempotency_claim
                        WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                          AND biz_id=:biz_id
                          AND state='active'
                          AND expires_at > now()
                        """
                    ),
                    {
                        "scope_kind": scope.kind,
                        "scope_id": scope.id,
                        "biz_id": biz_id,
                    },
                )
            ).scalar_one_or_none()
        return exists is not None

    async def verify_uncertain_effect(self, principal: UncertainEffectPrincipal) -> None:
        """仅接受数据库中仍有效的双人处置 generation，禁止伪造系统主体。"""

        async with self._engine().connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM sms_uncertain_resolution r
                        JOIN sms_chunk c ON c.id=r.chunk_id
                        WHERE r.id=:resolution_id
                          AND r.effect_generation=:generation
                          AND r.proposer_account_id=:proposer
                          AND r.confirmer_account_id=:confirmer
                          AND r.proposer_account_id <> r.confirmer_account_id
                          AND r.state IN (
                            'approved','effect_pending','applying',
                            'retryable_effect_error'
                          )
                          AND c.status='unknown_terminal'
                        """
                    ),
                    {
                        "resolution_id": principal.resolution_id,
                        "generation": principal.effect_generation,
                        "proposer": principal.proposer_account_id,
                        "confirmer": principal.confirmer_account_id,
                    },
                )
            ).scalar_one_or_none()
        if row is None:
            raise ValueError("system resend principal is not forgeable")

    async def exists(self, scope: IdempotencyScope, biz_id: str, batch_no: str) -> bool:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                text(
                    """
                        SELECT EXISTS (
                          SELECT 1 FROM idempotency_record i
                          JOIN sms_batch b ON b.id=i.batch_id
                          WHERE i.scope_kind=:scope_kind AND i.scope_id=:scope_id
                            AND i.biz_id=:biz_id
                            AND
                    """
                    + IDEMPOTENCY_LIVE_SQL
                    + """
                            AND b.batch_no=:batch_no
                        )
                    """
                ),
                {
                    "scope_kind": scope.kind,
                    "scope_id": scope.id,
                    "biz_id": biz_id,
                    "batch_no": batch_no,
                },
            )
            return bool(result.scalar_one())

    async def find_existing(self, scope: IdempotencyScope, biz_id: str) -> str | None:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                text(
                    """
                        SELECT b.batch_no FROM idempotency_record i
                        JOIN sms_batch b ON b.id=i.batch_id
                        WHERE i.scope_kind=:scope_kind AND i.scope_id=:scope_id
                          AND i.biz_id=:biz_id
                          AND
                    """
                    + IDEMPOTENCY_LIVE_SQL
                    + """
                    """
                ),
                {"scope_kind": scope.kind, "scope_id": scope.id, "biz_id": biz_id},
            )
            value = result.scalar_one_or_none()
            return str(value).strip() if value is not None else None

    async def find_request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyFingerprint | None:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT i.request_hash,i.request_hash_key_version
                    FROM idempotency_record i
                    JOIN sms_batch b ON b.id=i.batch_id
                    WHERE i.scope_kind=:scope_kind AND i.scope_id=:scope_id
                      AND i.biz_id=:biz_id
                      AND
                    """
                    + IDEMPOTENCY_LIVE_SQL
                    + """
                    """
                ),
                {"scope_kind": scope.kind, "scope_id": scope.id, "biz_id": biz_id},
            )
            row = result.mappings().one_or_none()
            if (
                row is None
                or row["request_hash"] is None
                or row["request_hash_key_version"] is None
            ):
                return None
            return IdempotencyFingerprint(
                str(row["request_hash"]).strip(),
                int(row["request_hash_key_version"]),
            )

    @staticmethod
    async def _insert(connection: AsyncConnection, command: BatchCommand, batch_no: str) -> int:
        if command.biz_id:
            await connection.execute(
                text(
                    """
                    DELETE FROM idempotency_record i
                    USING sms_batch b
                    WHERE i.batch_id=b.id
                      AND i.scope_kind=:scope_kind AND i.scope_id=:scope_id
                      AND i.biz_id=:biz_id
                      AND NOT
                    """
                    + IDEMPOTENCY_LIVE_SQL
                    + """
                    """
                ),
                {
                    "scope_kind": command.scope_kind,
                    "scope_id": command.scope_id,
                    "biz_id": command.biz_id,
                },
            )
        result = await connection.execute(
            text(
                """
                INSERT INTO sms_batch (
                  batch_no, category, channel, app_id, creator,
                  creator_account_id,creator_identity_id,dept, content,
                  display_content_enc,send_content_enc, sign_name, template_id, biz_id,
                  resend_of,
                  consent_confirmed,is_test,segments, quota_cost, status,remark,
                  deferred_reason, total, removed_duplicate,
                  removed_blacklist, removed_freq, scheduled_at,
                  usage_reservation_id
                ) VALUES (
                  :batch_no, :category, :channel, :app_id, :creator,
                  :creator_account_id,:creator_identity_id,:dept,'[encrypted]',
                  :display_content_enc,:send_content_enc, :sign_name, :template_id, :biz_id,
                  (SELECT id FROM sms_batch
                    WHERE batch_no=CAST(:resend_of AS char(32))),
                  :consent_confirmed,:is_test,:segments,:quota_cost,:status,
                  :remark,:deferred_reason,
                  :total, :removed_duplicate, :removed_blacklist,
                  :removed_freq, :scheduled_at, :usage_reservation_id
                ) RETURNING id
                """
            ),
            {
                "batch_no": batch_no,
                "category": command.category,
                "channel": command.channel,
                "app_id": command.app_id,
                "creator": (
                    command.principal.actor_name if command.principal is not None else None
                ),
                "creator_account_id": (
                    command.principal.account_id
                    if isinstance(
                        command.principal,
                        (SecurityPrincipal, UncertainEffectPrincipal),
                    )
                    else None
                ),
                "creator_identity_id": (
                    command.principal.identity_id
                    if isinstance(command.principal, SecurityPrincipal)
                    else None
                ),
                "dept": command.dept,
                "display_content_enc": command.display_content_enc,
                "send_content_enc": command.send_content_enc,
                "sign_name": command.sign_name,
                "template_id": command.template_id,
                "biz_id": command.biz_id,
                "resend_of": command.resend_of,
                "segments": command.segments,
                "is_test": command.is_test,
                "consent_confirmed": command.consent_confirmed,
                "remark": command.remark,
                "quota_cost": command.quota_cost,
                "status": command.status,
                "deferred_reason": command.deferred_reason,
                "total": len(command.messages),
                "removed_duplicate": command.removed_duplicate,
                "removed_blacklist": command.removed_blacklist,
                "removed_freq": command.removed_freq,
                "scheduled_at": command.scheduled_at,
                "usage_reservation_id": command.usage_reservation_id,
            },
        )
        batch_id = int(result.scalar_one())
        if command.resend_of is not None:
            action = await connection.execute(
                text(
                    """
                    INSERT INTO sms_resend_action(source_batch_id,child_batch_id)
                    SELECT resend_of,id FROM sms_batch
                    WHERE id=:batch_id AND resend_of IS NOT NULL
                    RETURNING source_batch_id
                    """
                ),
                {"batch_id": batch_id},
            )
            if action.scalar_one_or_none() is None:
                raise ValueError("失败重发源批次不存在")
        if command.usage_reservation_id is not None:
            await commit_usage_reservation(
                connection,
                reservation_id=command.usage_reservation_id,
                batch_id=batch_id,
            )
        if command.inflight_reservation_id is not None:
            from app.services.send_inflight import bind_in_flight_reservation

            if command.inflight_reservation_generation is None:
                raise ValueError("in-flight reservation generation missing")
            await bind_in_flight_reservation(
                connection,
                reservation_id=command.inflight_reservation_id,
                generation=command.inflight_reservation_generation,
                batch_id=batch_id,
                app_id=int(command.app_id or 0),
            )
        await connection.execute(
            text(
                """
                INSERT INTO sms_message (
                  batch_id, phone_enc, phone_hmac, phone_mask, key_version
                ) VALUES (
                  :batch_id, :phone_enc, :phone_hmac, :phone_mask, :key_version
                )
                """
            ),
            [
                {
                    "batch_id": batch_id,
                    "phone_enc": item.phone_enc,
                    "phone_hmac": item.phone_hmac,
                    "phone_mask": item.phone_mask,
                    "key_version": item.key_version,
                }
                for item in command.messages
            ],
        )
        if command.biz_id:
            if command.idempotency_claim_token and command.idempotency_claim_generation is not None:
                claimed = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT token, generation
                            FROM idempotency_claim
                            WHERE scope_kind=:scope_kind
                              AND scope_id=:scope_id
                              AND biz_id=:biz_id
                              AND state='active'
                              AND expires_at > now()
                            FOR UPDATE
                            """
                            ),
                            {
                                "scope_kind": command.scope_kind,
                                "scope_id": command.scope_id,
                                "biz_id": command.biz_id,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    claimed is None
                    or str(claimed["token"]).strip() != command.idempotency_claim_token
                    or int(claimed["generation"]) != command.idempotency_claim_generation
                ):
                    raise RuntimeError("idempotency claim lost")
            await connection.execute(
                text(
                    """
                    INSERT INTO idempotency_record (
                      app_id, scope_kind, scope_id, biz_id, batch_id,
                      request_hash, request_hash_key_version, expires_at
                    ) VALUES (
                      :app_id, :scope_kind, :scope_id, :biz_id, :batch_id,
                      :request_hash, :request_hash_key_version,
                      COALESCE((CAST(:scheduled_at AS timestamptz) + interval '7 days'),
                               now() + interval '24 hours')
                    )
                    """
                ),
                {
                    "app_id": command.app_id,
                    "scope_kind": command.scope_kind,
                    "scope_id": command.scope_id,
                    "biz_id": command.biz_id,
                    "batch_id": batch_id,
                    "request_hash": command.request_hash,
                    "request_hash_key_version": command.request_hash_key_version,
                    "scheduled_at": command.scheduled_at,
                },
            )
            if command.idempotency_claim_token and command.idempotency_claim_generation is not None:
                completed = (
                    await connection.execute(
                        text(
                            """
                            UPDATE idempotency_claim SET
                              state='completed',
                              batch_id=:batch_id,
                              completed_at=now(),
                              updated_at=now()
                            WHERE scope_kind=:scope_kind
                              AND scope_id=:scope_id
                              AND biz_id=:biz_id
                              AND token=:token
                              AND generation=:generation
                              AND state='active'
                              AND expires_at > now()
                            RETURNING id
                            """
                        ),
                        {
                            "scope_kind": command.scope_kind,
                            "scope_id": command.scope_id,
                            "biz_id": command.biz_id,
                            "token": command.idempotency_claim_token,
                            "generation": command.idempotency_claim_generation,
                            "batch_id": batch_id,
                        },
                    )
                ).scalar_one_or_none()
                if completed is None:
                    raise RuntimeError("idempotency claim lost")
        if command.status == "pending_approval":
            if not isinstance(command.principal, SecurityPrincipal):
                raise ValueError("待审批批次缺少稳定申请主体")
            if command.approval_threshold is None:
                raise ValueError("待审批批次缺少触发阈值快照")
            await connection.execute(
                text(
                    """
                    INSERT INTO approval(
                      batch_id,applicant,applicant_account_id,applicant_identity_id,
                      dept,trigger_threshold,
                      trigger_threshold_source,expires_at
                    ) VALUES(
                      :batch_id,:applicant,:applicant_account_id,:applicant_identity_id,
                      :dept,:trigger_threshold,'snapshot',
                      now()+make_interval(hours=>:expire_hours))
                    """
                ),
                {
                    "batch_id": batch_id,
                    "applicant": command.principal.login_name,
                    "applicant_account_id": command.principal.account_id,
                    "applicant_identity_id": command.principal.identity_id,
                    "dept": command.dept,
                    "trigger_threshold": command.approval_threshold,
                    "expire_hours": command.approval_expire_hours,
                },
            )
            await record_pending_approval_alert(
                connection,
                batch_no=batch_no,
                dept=command.dept,
                category=command.category,
                total=len(command.messages),
            )
        if command.status == "queued":
            queue = queue_for_category(command.category)
            await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="batch.ready",
                    aggregate_type="sms_batch",
                    aggregate_id=batch_no,
                    task_name="app.tasks.send.process_batch",
                    queue=queue,
                    args=(batch_no,),
                    dedup_key=f"batch.ready:{batch_no}",
                ),
            )
        await connection.execute(
            text(
                """
                INSERT INTO audit_log (
                  actor,actor_subject_kind,actor_account_id,actor_identity_id,
                  actor_app_id,role,action,object_type,object_id,after_val
                ) VALUES (
                  :actor,:actor_subject_kind,:actor_account_id,:actor_identity_id,
                  :actor_app_id,:role,'message_send','batch',:batch_no,
                  CAST(:after AS jsonb)
                )
                """
            ),
            {
                "actor": command.principal.actor_name,
                "actor_subject_kind": command.principal.subject_kind,
                "actor_account_id": command.principal.actor_account_id,
                "actor_identity_id": command.principal.actor_identity_id,
                "actor_app_id": command.principal.actor_app_id,
                "role": (
                    command.principal.role
                    if isinstance(command.principal, SecurityPrincipal)
                    else None
                ),
                "batch_no": batch_no,
                "after": json.dumps(
                    {
                        "batch_no": batch_no,
                        "phone_count": len(command.messages),
                        "consent_confirmed": command.consent_confirmed,
                    }
                ),
            },
        )
        if command.import_reservation_id is not None:
            if not isinstance(command.principal, SecurityPrincipal):
                raise ValueError("导入包预留缺少稳定操作主体")
            await consume_import_reservation(
                connection,
                reservation_id=command.import_reservation_id,
                batch_id=batch_id,
                principal=command.principal,
            )
        return batch_id

    async def save(self, command: BatchCommand) -> StoredBatch:
        batch_no = command.batch_no
        try:
            async with self._engine().begin() as connection:
                await self._insert(connection, command, batch_no)
            return StoredBatch(
                batch_no,
                False,
                outbox_persisted=command.status == "queued",
            )
        except IntegrityError:
            if command.biz_id:
                scope = IdempotencyScope(command.scope_kind, command.scope_id)
                existing = await self.find_existing(scope, command.biz_id)
                if existing is not None:
                    return StoredBatch(existing, True)
            if command.resend_of:
                existing_resend = await self.find_resend_child(command.resend_of)
                if existing_resend is not None:
                    return StoredBatch(existing_resend, True)
            raise

    async def find_resend_child(self, source_batch_no: str) -> str | None:
        """按唯一 resend_of 事实返回已创建的失败重发子批次。"""

        async with self._engine().connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT trim(child.batch_no)
                    FROM sms_resend_action action
                    JOIN sms_batch source ON source.id=action.source_batch_id
                    JOIN sms_batch child ON child.id=action.child_batch_id
                    WHERE source.batch_no=CAST(:source_batch_no AS char(32))
                    """
                ),
                {"source_batch_no": source_batch_no},
            )
            value = result.scalar_one_or_none()
            return str(value) if value is not None else None


class SqlTemplateRenderer:
    """只渲染厂商已审核通过的模板。"""

    def __init__(
        self,
        settings: Settings | None = None,
        crypto: CryptoService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.crypto = crypto

    def _crypto(self) -> CryptoService:
        if self.crypto is None:
            self.crypto = CryptoService.from_settings(self.settings)
        return self.crypto

    async def render(
        self,
        template_id: int,
        params: Sequence[str],
        dept: str,
    ) -> str:
        # 保留 dept 形参兼容发送端口；模板按平台 ID 全局解析。
        del dept
        binding_guard = (
            ""
            if bool(getattr(self.settings, "vendor_mock", False))
            else """AND vendor_template_id ~ '^[1-9][0-9]{0,9}$'
              AND (length(vendor_template_id)<10
                OR vendor_template_id<='2147483647')"""
        )
        async with database_engine(self.settings.database_url).connect() as connection:
            result = await connection.execute(
                text(
                    f"""
                        SELECT content_enc, var_specs FROM sms_template
                        WHERE id=:template_id
                          AND vendor_state='approved'
                          {binding_guard}
                        """
                ),
                {"template_id": template_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise ValueError("模板不存在或尚未审核通过")
            return render_template(
                decrypt_template_content(self._crypto(), row["content_enc"], template_id),
                cast(list[dict[str, object]], row["var_specs"] or []),
                params,
            )
