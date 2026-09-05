"""uncertain 保守终态后的双人处置；审批与 effect 分离，禁止改回 pending。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import (
    ActorPrincipal,
    SecurityPrincipal,
    UncertainEffectPrincipal,
)
from app.core.runtime_resources import database_engine
from app.services.crypto import CryptoService, EncryptionContext
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.pipeline import SendRequest
from app.services.usage_ledger import request_usage_release_for_batch
from app.services.usage_subject import (
    UncertainResendContext,
    UsageSubject,
    load_system_uncertain_resend_app,
    require_source_dept,
)
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
APPROVED_EFFECT_STATES = frozenset(
    {
        "approved",
        "effect_pending",
        "applying",
        "retryable_effect_error",
    }
)


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
    effect_generation: int = 1
    effect_error: str | None = None
    source_app_id: int | None = None
    source_channel: str | None = None
    source_category: str | None = None
    source_dept: str | None = None


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
                              proposer_account_id,confirmer_account_id,
                              child_batch_id,effect_generation,effect_error
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
    ) -> UncertainResolution:
        del actor
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                current = (
                    await connection.execute(
                        text(
                            """
                            SELECT r.id,r.chunk_id,r.batch_id,r.action,r.state,
                              r.proposer_account_id,r.confirmer_account_id,
                              r.child_batch_id,r.effect_generation,r.effect_error,
                              b.app_id,b.channel,b.category,b.dept
                            FROM sms_uncertain_resolution r
                            JOIN sms_batch b ON b.id=r.batch_id
                            WHERE r.id=:id FOR UPDATE
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
                try:
                    source_dept = require_source_dept(current["dept"])
                except ValueError as exc:
                    raise UncertainResolutionConflict(str(exc)) from exc
                updated = (
                    await connection.execute(
                        text(
                            """
                            UPDATE sms_uncertain_resolution
                            SET state='effect_pending',
                                confirmer_account_id=:account_id,
                                confirmed_at=now(),
                                approved_at=now(),
                                source_app_id=:app_id,
                                source_channel=:channel,
                                source_category=:category,
                                source_dept=:dept
                            WHERE id=:id AND state='proposed'
                            RETURNING id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,
                              child_batch_id,effect_generation,effect_error,
                              source_app_id,source_channel,source_category,
                              source_dept
                            """
                        ),
                        {
                            "id": resolution_id,
                            "account_id": principal.account_id,
                            "app_id": current["app_id"],
                            "channel": current["channel"],
                            "category": current["category"],
                            "dept": source_dept,
                        },
                    )
                ).mappings().one_or_none()
                if updated is None:
                    raise UncertainResolutionConflict("处置单已确认")
                generation = int(updated["effect_generation"])
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="uncertain.effect",
                        aggregate_type="sms_uncertain_resolution",
                        aggregate_id=str(resolution_id),
                        task_name="app.tasks.outbox.apply_uncertain_effect",
                        queue="realtime",
                        args=(resolution_id,),
                        dedup_key=f"uncertain.effect:{resolution_id}:{generation}",
                    ),
                )
                return _row(updated)
        finally:
            await engine.dispose()

    async def apply_effect(self, resolution_id: int) -> UncertainResolution:
        """由 Outbox worker 幂等执行已批准处置；HTTP 路径不得直接调用 Pipeline。"""

        current = await self._mark_applying(resolution_id)
        if current.state in {"effect_applied", "closed"}:
            return current
        try:
            if current.action == "confirm_not_accepted":
                await self._run_not_accepted(current)
            elif current.action == "resend_new_batch":
                await self._run_resend(current)
            return await self._mark_closed(resolution_id)
        except Exception as exc:
            if _is_retryable_effect_error(exc):
                await self._mark_retryable(resolution_id)
            else:
                await self._mark_manual(resolution_id, _manual_effect_error(exc))
            raise

    async def _mark_applying(self, resolution_id: int) -> UncertainResolution:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                current = (
                    await connection.execute(
                        text(
                            """
                            SELECT id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,
                              child_batch_id,effect_generation,effect_error,
                              source_app_id,source_channel,source_category,
                              source_dept
                            FROM sms_uncertain_resolution
                            WHERE id=:id FOR UPDATE
                            """
                        ),
                        {"id": resolution_id},
                    )
                ).mappings().one_or_none()
                if current is None:
                    raise UncertainResolutionNotFound
                if str(current["state"]) in {"effect_applied", "closed"}:
                    return _row(current)
                if str(current["state"]) not in APPROVED_EFFECT_STATES:
                    raise UncertainResolutionConflict("处置单不可执行")
                updated = (
                    await connection.execute(
                        text(
                            """
                            UPDATE sms_uncertain_resolution
                            SET state='applying', effect_error=NULL
                            WHERE id=:id AND state=ANY(:states)
                            RETURNING id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,
                              child_batch_id,effect_generation,effect_error
                            """
                        ),
                        {
                            "id": resolution_id,
                            "states": list(APPROVED_EFFECT_STATES),
                        },
                    )
                ).mappings().one_or_none()
                if updated is None:
                    raise UncertainResolutionConflict("处置单不可执行")
                merged = dict(updated)
                merged["source_app_id"] = current["source_app_id"]
                merged["source_channel"] = current["source_channel"]
                merged["source_category"] = current["source_category"]
                merged["source_dept"] = current["source_dept"]
                return _row(merged)
        finally:
            await engine.dispose()

    async def _run_not_accepted(self, current: UncertainResolution) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await _apply_not_accepted(
                    connection,
                    resolution_id=current.id,
                    chunk_id=current.chunk_id,
                    batch_id=current.batch_id,
                )
        finally:
            await engine.dispose()

    async def _run_resend(self, current: UncertainResolution) -> None:
        engine = self._engine()
        recovered_child = False
        child_id: int | None = None
        app_ctx: ApiAppContext | None = None
        request: SendRequest | None = None
        try:
            async with engine.begin() as connection:
                existing = (
                    await connection.execute(
                        text(
                            """
                            SELECT child_batch_id, generation
                            FROM sms_uncertain_child
                            WHERE resolution_id=:id
                            """
                        ),
                        {"id": current.id},
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    if int(existing["generation"]) != current.effect_generation:
                        raise UncertainResolutionConflict("处置 generation 已变化")
                    child_id = int(existing["child_batch_id"])
                elif current.child_batch_id is not None:
                    child_id = int(current.child_batch_id)
                else:
                    recovered = await _lookup_child_by_biz_id(
                        connection,
                        resolution_id=current.id,
                        generation=current.effect_generation,
                    )
                    if recovered is not None:
                        child_id = recovered
                        recovered_child = True
                    else:
                        context, app_ctx = await _load_resend_context(connection, current)
                        request = await self._build_resend(
                            connection,
                            chunk_id=current.chunk_id,
                            resolution_id=current.id,
                            generation=current.effect_generation,
                            actor=_effect_principal(current),
                            usage_subject=context.usage_subject,
                        )
            if request is not None and app_ctx is not None:
                from app.api.messages import _pipeline

                pipeline = await _pipeline(app_ctx)
                result = await pipeline.accept(app_ctx, request)
                async with engine.begin() as connection:
                    found = (
                        await connection.execute(
                            text("SELECT id FROM sms_batch WHERE batch_no=:batch_no"),
                            {"batch_no": result.batch_no},
                        )
                    ).scalar_one_or_none()
                    if found is None:
                        found = await _lookup_child_by_biz_id(
                            connection,
                            resolution_id=current.id,
                            generation=current.effect_generation,
                        )
                        recovered_child = found is not None
                    if found is None:
                        raise UncertainResolutionConflict("重发子批次缺失")
                    child_id = int(found)
            if child_id is None:
                raise UncertainResolutionConflict("重发子批次缺失")
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_uncertain_child (
                          resolution_id, child_batch_id, generation, recovered
                        ) VALUES (:id,:child_id,:generation,:recovered)
                        ON CONFLICT (resolution_id) DO NOTHING
                        """
                    ),
                    {
                        "id": current.id,
                        "child_id": child_id,
                        "generation": current.effect_generation,
                        "recovered": recovered_child,
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE sms_uncertain_resolution
                        SET child_batch_id=:child_id
                        WHERE id=:id AND child_batch_id IS NULL
                        """
                    ),
                    {"id": current.id, "child_id": child_id},
                )
        finally:
            await engine.dispose()

    async def _mark_closed(self, resolution_id: int) -> UncertainResolution:
        return await self._set_state(
            resolution_id,
            "closed",
            extra="effect_applied_at=now(), effect_error=NULL",
            from_states=("applying", "effect_applied"),
        )

    async def _mark_manual(
        self,
        resolution_id: int,
        effect_error: str = "source_context_invalid",
    ) -> None:
        safe_error = "".join(
            char for char in effect_error if char.isalnum() or char in {"_", "-"}
        )[:128] or "source_context_invalid"
        await self._set_state(
            resolution_id,
            "manual_intervention_required",
            extra=f"effect_error='{safe_error}'",
            from_states=("applying",),
        )

    async def _mark_retryable(self, resolution_id: int) -> None:
        await self._set_state(
            resolution_id,
            "retryable_effect_error",
            extra="effect_error='retryable_effect_error'",
            from_states=("applying",),
        )

    async def _set_state(
        self,
        resolution_id: int,
        state: str,
        *,
        extra: str,
        from_states: tuple[str, ...],
    ) -> UncertainResolution:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = (
                    await connection.execute(
                        text(
                            f"""
                            UPDATE sms_uncertain_resolution
                            SET state=:state, {extra}
                            WHERE id=:id AND state=ANY(:from_states)
                            RETURNING id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,
                              child_batch_id,effect_generation,effect_error
                            """
                        ),
                        {
                            "id": resolution_id,
                            "state": state,
                            "from_states": list(from_states),
                        },
                    )
                ).mappings().one_or_none()
                if updated is None:
                    current = (
                        await connection.execute(
                            text(
                                """
                                SELECT id,chunk_id,batch_id,action,state,
                                  proposer_account_id,confirmer_account_id,
                                  child_batch_id,effect_generation,effect_error
                                FROM sms_uncertain_resolution
                                WHERE id=:id
                                """
                            ),
                            {"id": resolution_id},
                        )
                    ).mappings().one()
                    if str(current["state"]) == state:
                        return _row(current)
                    raise UncertainResolutionConflict("处置单状态已变化")
                return _row(updated)
        finally:
            await engine.dispose()

    async def _build_resend(
        self,
        connection: Any,
        *,
        chunk_id: int,
        resolution_id: int,
        generation: int,
        actor: UncertainEffectPrincipal,
        usage_subject: UsageSubject,
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
        bound = UncertainEffectPrincipal(
            resolution_id=actor.resolution_id,
            proposer_account_id=actor.proposer_account_id,
            confirmer_account_id=actor.confirmer_account_id,
            effect_generation=actor.effect_generation,
            dept=str(batch["dept"])[:128],
        )
        if bound.resolution_id != resolution_id or bound.effect_generation != generation:
            raise UncertainResolutionConflict("处置 generation 已变化")
        return SendRequest(
            category=str(batch["category"]),
            mobiles=mobiles,
            content=content,
            sign_name=str(batch["sign_name"]) if batch["sign_name"] is not None else None,
            channel=str(batch["channel"]),
            consent_confirmed=bool(batch["consent_confirmed"]),
            actor=bound,
            biz_id=f"manual-resend:{resolution_id}:{generation}"[:32],
            is_test=bool(batch["is_test"]),
            resend_dept=str(batch["dept"]),
            usage_subject=usage_subject,
        )


def _effect_principal(current: Any) -> UncertainEffectPrincipal:
    if isinstance(current, UncertainResolution):
        confirmer = current.confirmer_account_id
        proposer = current.proposer_account_id
        resolution_id = current.id
        generation = current.effect_generation
        dept = current.source_dept
    else:
        confirmer = current.get("confirmer_account_id")
        proposer = current.get("proposer_account_id")
        resolution_id = int(current["id"])
        generation = int(current.get("effect_generation") or 1)
        dept = current.get("source_dept") or current.get("dept")
    if confirmer is None:
        raise UncertainResolutionConflict("重发缺少确认人")
    try:
        bound_dept = require_source_dept(dept)
    except ValueError as exc:
        raise UncertainResolutionConflict(str(exc)) from exc
    return UncertainEffectPrincipal(
        resolution_id=int(resolution_id),
        proposer_account_id=int(proposer),
        confirmer_account_id=int(confirmer),
        effect_generation=int(generation),
        dept=bound_dept,
    )


async def _load_resend_context(
    connection: AsyncConnection,
    current: UncertainResolution,
) -> tuple[UncertainResendContext, ApiAppContext]:
    """锁定已批准 resolution 后构造不可伪造的内部重发上下文。"""

    await _require_active_dual_control(connection, current)
    source_dept = _resolution_source_dept(current)
    source_channel = str(current.source_channel or "")
    source_category = str(current.source_category or "")
    if source_category not in {"verify", "notice", "market"}:
        raise UncertainResolutionConflict("源类别无法恢复")
    if source_channel == "api":
        loaded = await _load_source_api_app(
            connection,
            current.source_app_id,
            source_category,
        )
        app_ctx = ApiAppContext(
            loaded.app_id,
            loaded.name,
            source_dept,
            loaded.allowed_categories,
            loaded.default_sign,
            loaded.daily_quota,
            loaded.blacklist_check,
            loaded.freq_override,
            loaded.rate_limit_per_min,
            loaded.allowed_ips,
            loaded.recipient_limit_per_min,
            loaded.segment_limit_per_min,
            loaded.max_in_flight_chunks,
            loaded.allow_market_api_bulk,
            loaded.ip_allowlist_exempt_until,
            loaded.unlimited_quota_exempt_until,
        )
        usage = UsageSubject(
            kind="api_app",
            app_id=app_ctx.app_id,
            dept=source_dept,
            category=source_category,
            resolution_id=current.id,
            effect_generation=current.effect_generation,
        )
        source_app_id = app_ctx.app_id
    elif source_channel == "web":
        try:
            system_app = await load_system_uncertain_resend_app(connection)
        except LookupError as exc:
            raise UncertainResolutionConflict("system usage subject unavailable") from exc
        if source_category not in system_app.allowed_categories:
            raise UncertainResolutionConflict("源应用类别权限已收回")
        app_ctx = ApiAppContext(
            system_app.app_id,
            system_app.name,
            source_dept,
            system_app.allowed_categories,
            daily_quota=system_app.daily_quota,
            blacklist_check=system_app.blacklist_check,
            rate_limit_per_min=system_app.rate_limit_per_min,
            max_in_flight_chunks=system_app.max_in_flight_chunks,
        )
        usage = UsageSubject(
            kind="system_effect",
            app_id=system_app.app_id,
            dept=source_dept,
            category=source_category,
            resolution_id=current.id,
            effect_generation=current.effect_generation,
        )
        source_app_id = current.source_app_id
    else:
        raise UncertainResolutionConflict("源渠道无法恢复")
    return (
        UncertainResendContext(
            resolution_id=current.id,
            effect_generation=current.effect_generation,
            proposer_account_id=current.proposer_account_id,
            confirmer_account_id=int(current.confirmer_account_id),
            source_batch_id=current.batch_id,
            source_chunk_id=current.chunk_id,
            source_channel=source_channel,
            source_app_id=source_app_id,
            source_dept=source_dept,
            source_category=source_category,
            usage_subject=usage,
        ),
        app_ctx,
    )


async def _load_source_api_app(
    connection: AsyncConnection,
    app_id: int | None,
    category: str,
) -> ApiAppContext:
    if app_id is None or app_id < 1:
        raise UncertainResolutionConflict("源应用不可用")
    row = (
        await connection.execute(
            text(
                """
                SELECT id,name,dept,allowed_categories,daily_quota,status,
                       unlimited_quota_exempt_until,max_in_flight_chunks,
                       rate_limit_per_min,blacklist_check
                FROM app
                WHERE id=:id AND usage_subject_kind='api_app'
                """
            ),
            {"id": int(app_id)},
        )
    ).mappings().one_or_none()
    if row is None or int(row["status"]) != 1:
        raise UncertainResolutionConflict("源应用不可用")
    categories = frozenset(
        item.strip()
        for item in str(row["allowed_categories"]).split(",")
        if item.strip()
    )
    if category and category not in categories:
        raise UncertainResolutionConflict("源应用类别权限已收回")
    return ApiAppContext(
        int(row["id"]),
        str(row["name"]),
        str(row["dept"]),
        categories,
        daily_quota=int(row["daily_quota"]),
        blacklist_check=bool(row["blacklist_check"]),
        rate_limit_per_min=int(row["rate_limit_per_min"]),
        max_in_flight_chunks=int(row["max_in_flight_chunks"]),
        unlimited_quota_exempt_until=row["unlimited_quota_exempt_until"],
    )


def _resolution_source_dept(current: UncertainResolution) -> str:
    try:
        return require_source_dept(current.source_dept)
    except ValueError as exc:
        raise UncertainResolutionConflict(str(exc)) from exc


async def _require_active_dual_control(
    connection: AsyncConnection,
    current: UncertainResolution,
) -> None:
    if current.confirmer_account_id is None:
        raise UncertainResolutionConflict("重发缺少确认人")
    rows = (
        await connection.execute(
            text(
                """
                SELECT id,status,role
                FROM user_account
                WHERE id IN (:proposer, :confirmer)
                """
            ),
            {
                "proposer": current.proposer_account_id,
                "confirmer": current.confirmer_account_id,
            },
        )
    ).mappings()
    by_id = {int(row["id"]): row for row in rows}
    for account_id in (current.proposer_account_id, current.confirmer_account_id):
        row = by_id.get(int(account_id))
        if row is None or int(row["status"]) != 1 or str(row["role"]) != "admin":
            raise UncertainResolutionConflict("确认人或提案人已失效")


async def _lookup_child_by_biz_id(
    connection: AsyncConnection,
    *,
    resolution_id: int,
    generation: int,
) -> int | None:
    biz_id = f"manual-resend:{resolution_id}:{generation}"[:32]
    found = (
        await connection.execute(
            text(
                """
                SELECT b.id
                FROM sms_batch b
                JOIN idempotency_record i ON i.batch_id=b.id
                WHERE i.scope_kind='uncertain-resend'
                  AND i.scope_id=:scope_id
                  AND i.biz_id=:biz_id
                """
            ),
            {"scope_id": str(resolution_id), "biz_id": biz_id},
        )
    ).scalar_one_or_none()
    return int(found) if found is not None else None


def _is_retryable_effect_error(exc: BaseException) -> bool:
    if isinstance(exc, UncertainResolutionConflict):
        return False
    from app.services.pipeline import AllFiltered, ConsentRequired, SensitiveWord
    from app.services.usage_ledger import UsageReservationConflict

    return not isinstance(
        exc,
        (
            AllFiltered,
            SensitiveWord,
            ConsentRequired,
            UsageReservationConflict,
            ValueError,
            LookupError,
        ),
    )


def _manual_effect_error(exc: BaseException) -> str:
    text_value = str(exc).casefold()
    if "generation" in text_value:
        return "generation_mismatch"
    if "类别" in str(exc) or "category" in text_value:
        return "source_category_invalid"
    if "应用" in str(exc) or "source app" in text_value:
        return "source_app_invalid"
    if "确认人" in str(exc) or "提案" in str(exc) or "失效" in str(exc):
        return "actor_invalid"
    if "usage" in text_value or "subject" in text_value or "dept" in text_value:
        return "usage_subject_invalid"
    return "source_context_invalid"


async def _apply_not_accepted(
    connection: AsyncConnection,
    *,
    resolution_id: int,
    chunk_id: int,
    batch_id: int,
) -> None:
    allocation = (
        await connection.execute(
            text(
                """
                SELECT reservation_id,recipient_count,segment_count,request_count
                FROM usage_chunk_allocation WHERE chunk_id=:chunk_id
                """
            ),
            {"chunk_id": chunk_id},
        )
    ).mappings().one_or_none()
    recipient_count = int(allocation["recipient_count"]) if allocation else 0
    segment_count = int(allocation["segment_count"]) if allocation else 0
    request_count = int(allocation["request_count"]) if allocation else 0
    reservation_id = allocation["reservation_id"] if allocation else None
    await connection.execute(
        text(
            """
            INSERT INTO usage_chunk_release (
              resolution_id,chunk_id,reservation_id,recipient_count,
              segment_count,request_count,release_event_id
            ) VALUES (
              :resolution_id,:chunk_id,:reservation_id,:recipients,
              :segments,:requests,:event_id
            )
            ON CONFLICT (resolution_id) DO NOTHING
            """
        ),
        {
            "resolution_id": resolution_id,
            "chunk_id": chunk_id,
            "reservation_id": reservation_id,
            "recipients": recipient_count,
            "segments": segment_count,
            "requests": request_count,
            "event_id": f"resolution:{resolution_id}:not-accepted",
        },
    )
    if not await _all_chunks_not_accepted(connection, batch_id):
        return
    reservation = (
        await connection.execute(
            text("SELECT usage_reservation_id FROM sms_batch WHERE id=:batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one_or_none()
    if reservation is None:
        return
    await request_usage_release_for_batch(
        connection,
        batch_id=batch_id,
        event_id=f"usage:{reservation}:uncertain-unused",
    )


async def _all_chunks_not_accepted(
    connection: AsyncConnection,
    batch_id: int,
) -> bool:
    """只有整批所有分片都被证明未受理时才允许整批释放。"""

    leftover = await connection.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1 FROM sms_chunk
              WHERE batch_id=:batch_id
                AND status IN ('submitted','submitting','pending','retrying',
                               'uncertain','unknown_terminal')
                AND NOT EXISTS (
                  SELECT 1 FROM usage_chunk_release r
                  WHERE r.chunk_id=sms_chunk.id
                )
            )
            OR EXISTS (
              SELECT 1 FROM sms_message
              WHERE batch_id=:batch_id AND status IN ('sent','delivered')
            )
            """
        ),
        {"batch_id": batch_id},
    )
    return not bool(leftover.scalar_one())


def _row(row: Any) -> UncertainResolution:
    source_app = row.get("source_app_id")
    source_channel = row.get("source_channel")
    source_category = row.get("source_category")
    source_dept = row.get("source_dept")
    return UncertainResolution(
        int(row["id"]),
        int(row["chunk_id"]),
        int(row["batch_id"]),
        str(row["action"]),
        str(row["state"]),
        int(row["proposer_account_id"]),
        int(row["confirmer_account_id"]) if row["confirmer_account_id"] is not None else None,
        int(row["child_batch_id"]) if row["child_batch_id"] is not None else None,
        int(row["effect_generation"]) if row.get("effect_generation") is not None else 1,
        str(row["effect_error"]) if row.get("effect_error") is not None else None,
        int(source_app) if source_app is not None else None,
        str(source_channel) if source_channel is not None else None,
        str(source_category) if source_category is not None else None,
        str(source_dept) if source_dept is not None else None,
    )
