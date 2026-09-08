"""uncertain 保守终态后的双人处置；审批与 effect 分离，禁止改回 pending。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.apikey import APP_SEND_POLICY_COLUMNS, ApiAppContext, load_app_send_policy
from app.core.auth.accounts import (
    ActorPrincipal,
    SecurityPrincipal,
    UncertainEffectPrincipal,
)
from app.core.runtime_resources import database_engine
from app.services.crypto import CryptoService, EncryptionContext
from app.services.idempotency import IdempotencyScope, uncertain_resend_biz_id
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.pipeline import SendRequest
from app.services.uncertain_source import prepare_source_proof
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


class SourceMessageStateChanged(UncertainResolutionConflict):
    """源消息或其接收人证明已变化，需要重新人工判断。"""


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
                await _lock_resolution_source(connection, resolution_id)
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
                            WHERE r.id=:id
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
            if current.action == "confirm_not_accepted":
                # 已确认但整批暂缓时，重复受控 effect 重新结算同一事实。
                await self._run_not_accepted(current)
            return current
        try:
            if current.action == "confirm_not_accepted":
                await self._run_not_accepted(current)
            elif current.action == "resend_new_batch":
                child_id = await self._run_resend(current)
                return await self._close_resend(current, child_id)
            return await self._mark_closed(resolution_id, generation=current.effect_generation)
        except Exception as exc:
            if _is_retryable_effect_error(exc):
                await self._mark_retryable(resolution_id, generation=current.effect_generation)
            else:
                await self._mark_manual(
                    resolution_id, _manual_effect_error(exc), generation=current.effect_generation
                )
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
                    generation=current.effect_generation,
                )
        finally:
            await engine.dispose()

    async def _run_resend(self, current: UncertainResolution) -> int:
        """只恢复已证明的内部 child；同名或结果不明的旧记录不能触发新发送。"""

        from app.api.messages import _pipeline

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                locked, context, app_ctx = await lock_uncertain_resend(
                    connection, _effect_principal(current)
                )
                relation = await _child_relation(connection, locked.id)
                if relation is not None and bool(relation["provenance_verified"]):
                    _check_child_context(relation, locked, context)
                    return int(relation["child_batch_id"])
                candidates = await _legacy_resend_candidates(connection, locked)
                request = await self._build_resend(
                    connection,
                    chunk_id=locked.chunk_id,
                    resolution_id=locked.id,
                    generation=locked.effect_generation,
                    actor=_effect_principal(locked),
                    usage_subject=context.usage_subject,
                )
                if candidates:
                    if len(candidates) != 1:
                        raise UncertainResolutionConflict("重发子批次来源冲突")
                    candidate = candidates[0]
                    _check_child_context(candidate, locked, context)
                    if not bool(candidate["system_audit"]):
                        raise UncertainResolutionConflict("重发子批次来源无法证明")
                    pipeline = await _pipeline(app_ctx)
                    legacy_request = replace(request, biz_id=str(candidate["biz_id"]))
                    try:
                        await pipeline._ensure_same_request(
                            IdempotencyScope("uncertain-resend", str(locked.id)),
                            str(candidate["biz_id"]),
                            legacy_request,
                            app_ctx,
                            pipeline._resolve_policy(app_ctx, legacy_request, None),
                        )
                    except Exception as exc:
                        raise UncertainResolutionConflict("重发子批次指纹无法证明") from exc
                    child_id = int(candidate["child_batch_id"])
                    await bind_uncertain_child(connection, locked, child_id, recovered=True)
                    return child_id
                if relation is not None or locked.child_batch_id is not None:
                    raise UncertainResolutionConflict("重发子批次来源无法证明")
            pipeline = await _pipeline(app_ctx)
            # COMMIT 响应丢失由下一次 effect 读取原子关系，不猜测返回的 batch_no。
            await pipeline.accept(app_ctx, request)
            async with engine.begin() as connection:
                locked, context, _app_ctx = await lock_uncertain_resend(
                    connection, _effect_principal(current)
                )
                relation = await _child_relation(connection, locked.id)
                if relation is None or not bool(relation["provenance_verified"]):
                    raise UncertainResolutionConflict("重发子批次缺少可信创建关系")
                _check_child_context(relation, locked, context)
                return int(relation["child_batch_id"])
        finally:
            await engine.dispose()

    async def _close_resend(
        self, current: UncertainResolution, child_id: int
    ) -> UncertainResolution:
        """关闭 CAS 绑定已核验 child 和 generation，旧执行者不能关闭新处置。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                row = (
                    (
                        await connection.execute(
                            text("""
                    UPDATE sms_uncertain_resolution r
                    SET state='closed', effect_applied_at=now(), effect_error=NULL
                    WHERE r.id=:id AND r.effect_generation=:generation
                      AND r.child_batch_id=:child_id
                      AND r.state IN ('applying','effect_applied','closed')
                      AND EXISTS (SELECT 1 FROM sms_uncertain_child c
                        WHERE c.resolution_id=r.id AND c.child_batch_id=:child_id
                          AND c.generation=:generation AND c.provenance_verified)
                    RETURNING r.*
                """),
                            {
                                "id": current.id,
                                "generation": current.effect_generation,
                                "child_id": child_id,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise UncertainResolutionConflict("处置 generation 或 child 已变化")
                return _row(row)
        finally:
            await engine.dispose()

    async def _mark_closed(
        self, resolution_id: int, *, generation: int | None = None,
    ) -> UncertainResolution:
        return await self._set_state(
            resolution_id,
            "closed",
            extra="effect_applied_at=now(), effect_error=NULL",
            from_states=("applying", "effect_applied"),
            expected_generation=generation,
        )

    async def _mark_manual(
        self,
        resolution_id: int,
        effect_error: str = "source_context_invalid",
        *,
        generation: int | None = None,
    ) -> None:
        safe_error = "".join(
            char for char in effect_error if char.isalnum() or char in {"_", "-"}
        )[:128] or "source_context_invalid"
        await self._set_state(
            resolution_id,
            "manual_intervention_required",
            extra=f"effect_error='{safe_error}'",
            from_states=("applying",),
            expected_generation=generation,
        )

    async def _mark_retryable(self, resolution_id: int, *, generation: int | None = None) -> None:
        await self._set_state(
            resolution_id,
            "retryable_effect_error",
            extra="effect_error='retryable_effect_error'",
            from_states=("applying",),
            expected_generation=generation,
        )

    async def _set_state(
        self,
        resolution_id: int,
        state: str,
        *,
        extra: str,
        from_states: tuple[str, ...],
        expected_generation: int | None = None,
    ) -> UncertainResolution:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = (
                    (
                        await connection.execute(
                            text(
                                f"""
                            UPDATE sms_uncertain_resolution
                            SET state=:state, {extra}
                            WHERE id=:id AND state=ANY(:from_states)
                              AND (CAST(:generation AS integer) IS NULL
                                   OR effect_generation=:generation)
                            RETURNING id,chunk_id,batch_id,action,state,
                              proposer_account_id,confirmer_account_id,
                              child_batch_id,effect_generation,effect_error
                            """
                            ),
                            {
                                "id": resolution_id,
                                "state": state,
                                "from_states": list(from_states),
                                "generation": expected_generation,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
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
                    SELECT id,created_at,batch_id,chunk_id,status,report_status,
                      report_time,report_event_key,phone_enc,trim(phone_hmac) phone_hmac,key_version
                    FROM sms_message
                    WHERE chunk_id=:chunk_id AND status='unknown'
                      AND report_status IS DISTINCT FROM 1
                    ORDER BY id,created_at
                    """
                ),
                {"chunk_id": chunk_id},
            )
        ).mappings().all()
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
            biz_id=uncertain_resend_biz_id(resolution_id, generation),
            is_test=bool(batch["is_test"]),
            resend_dept=str(batch["dept"]),
            usage_subject=usage_subject,
            uncertain_source_proof=prepare_source_proof(
                self.crypto, resolution_id, generation, phones, mobiles,
            ),
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
    confirmer_id = current.confirmer_account_id
    if confirmer_id is None:
        raise UncertainResolutionConflict("重发缺少确认人")
    source_dept = _resolution_source_dept(current)
    source_channel = str(current.source_channel or "")
    source_category = str(current.source_category or "")
    source_app_id: int | None
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
            # 源请求签名已固化；空签名不得被后来新增的默认签名覆盖。
            None,
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
            confirmer_account_id=int(confirmer_id),
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
                f"""
                SELECT {APP_SEND_POLICY_COLUMNS}, status
                FROM app
                WHERE id=:id AND usage_subject_kind='api_app'
                """
            ),
            {"id": int(app_id)},
        )
    ).mappings().one_or_none()
    if row is None or int(row["status"]) != 1:
        raise UncertainResolutionConflict("源应用不可用")
    try:
        policy = load_app_send_policy(dict(row))
    except (KeyError, TypeError, ValueError) as exc:
        raise UncertainResolutionConflict("源应用发送策略不可用") from exc
    categories = frozenset(item.strip() for item in policy.pop("allowed_categories").split(","))
    if category and category not in categories:
        raise UncertainResolutionConflict("源应用类别权限已收回")
    return ApiAppContext(**policy, allowed_categories=categories)



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


async def _lock_resolution_source(connection: AsyncConnection, resolution_id: int) -> None:
    """所有多表处置路径与报告统一为 chunk→batch→resolution，禁止反向等待。"""
    identity = (await connection.execute(text(
        "SELECT chunk_id,batch_id FROM sms_uncertain_resolution WHERE id=:id"
    ), {"id": resolution_id})).mappings().one_or_none()
    if identity is None:
        raise UncertainResolutionNotFound
    for table, key in (("sms_chunk", "chunk_id"), ("sms_batch", "batch_id")):
        await connection.execute(text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE"),
                                 {"id": identity[key]})
    await connection.execute(text(
        "SELECT id FROM sms_uncertain_resolution WHERE id=:id FOR UPDATE"
    ), {"id": resolution_id})
    current = (await connection.execute(text(
        "SELECT chunk_id,batch_id FROM sms_uncertain_resolution WHERE id=:id"
    ), {"id": resolution_id})).mappings().one_or_none()
    if current is None or dict(current) != dict(identity):
        raise UncertainResolutionConflict("重发源上下文已变化")


async def lock_uncertain_resend(
    connection: AsyncConnection,
    principal: UncertainEffectPrincipal,
) -> tuple[UncertainResolution, UncertainResendContext, ApiAppContext]:
    """在创建/恢复事务锁定批准事实及源分片，校验完整来源与当前执行者。"""

    await _lock_resolution_source(connection, principal.resolution_id)
    row = (
        (
            await connection.execute(
                text("""
        SELECT r.*, c.status AS chunk_status, c.batch_id AS chunk_batch_id,
          b.app_id AS actual_app_id, b.dept AS actual_dept,
          b.channel AS actual_channel, b.category AS actual_category
        FROM sms_uncertain_resolution r
        JOIN sms_chunk c ON c.id=r.chunk_id
        JOIN sms_batch b ON b.id=r.batch_id
        WHERE r.id=:id
    """),
                {"id": principal.resolution_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise UncertainResolutionConflict("处置单不存在")
    current = _row(row)
    if current.effect_generation != principal.effect_generation:
        raise UncertainResolutionConflict("处置 generation 已变化")
    if (
        current.state not in APPROVED_EFFECT_STATES
        or current.action != "resend_new_batch"
        or current.proposer_account_id != principal.proposer_account_id
        or current.confirmer_account_id != principal.confirmer_account_id
        or current.proposer_account_id == current.confirmer_account_id
        or current.source_dept != principal.dept
        or row["chunk_status"] != "unknown_terminal"
        or int(row["chunk_batch_id"]) != current.batch_id
        or row["actual_app_id"] != current.source_app_id
        or row["actual_dept"] != current.source_dept
        or row["actual_channel"] != current.source_channel
        or row["actual_category"] != current.source_category
    ):
        raise UncertainResolutionConflict("重发源上下文或批准事实已变化")
    context, app_ctx = await _load_resend_context(connection, current)
    return current, context, app_ctx


async def _child_relation(connection: AsyncConnection, resolution_id: int) -> Any:
    return (
        (
            await connection.execute(
                text("""
        SELECT c.*, b.app_id,b.dept,b.category,b.channel,b.creator
        FROM sms_uncertain_child c JOIN sms_batch b ON b.id=c.child_batch_id
        WHERE c.resolution_id=:id
    """),
                {"id": resolution_id},
            )
        )
        .mappings()
        .one_or_none()
    )


def _check_child_context(
    row: Any, current: UncertainResolution, context: UncertainResendContext
) -> None:
    if int(row["generation"]) != current.effect_generation:
        raise UncertainResolutionConflict("处置 generation 已变化")
    if (
        row["app_id"] != context.usage_subject.app_id
        or row["dept"] != current.source_dept
        or row["channel"] != current.source_channel
        or row["category"] != current.source_category
        or row["creator"] != _effect_principal(current).actor_name
        or (current.child_batch_id is not None and current.child_batch_id != row["child_batch_id"])
    ):
        raise UncertainResolutionConflict("重发子批次来源冲突")


async def bind_uncertain_child(
    connection: AsyncConnection,
    current: UncertainResolution,
    child_id: int,
    *,
    recovered: bool,
) -> None:
    """必须与 batch/Outbox 或已证明的历史来源同事务写关系，并核对冲突胜者。"""

    await connection.execute(
        text("""
        INSERT INTO sms_uncertain_child
          (resolution_id,child_batch_id,generation,recovered,provenance_verified)
        VALUES (:id,:child_id,:generation,:recovered,true)
        ON CONFLICT (resolution_id) DO NOTHING
    """),
        {
            "id": current.id,
            "child_id": child_id,
            "generation": current.effect_generation,
            "recovered": recovered,
        },
    )
    winner = await _child_relation(connection, current.id)
    if (
        winner is None
        or int(winner["child_batch_id"]) != child_id
        or int(winner["generation"]) != current.effect_generation
    ):
        raise UncertainResolutionConflict("处置 generation 或 child 已变化")
    await connection.execute(
        text("""
        UPDATE sms_uncertain_child SET provenance_verified=true
        WHERE resolution_id=:id AND child_batch_id=:child_id AND generation=:generation
    """),
        {"id": current.id, "child_id": child_id, "generation": current.effect_generation},
    )
    bound = await connection.execute(
        text("""
        UPDATE sms_uncertain_resolution SET child_batch_id=:child_id
        WHERE id=:id AND effect_generation=:generation
          AND state=ANY(:states) AND (child_batch_id IS NULL OR child_batch_id=:child_id)
        RETURNING id
    """),
        {
            "id": current.id,
            "child_id": child_id,
            "generation": current.effect_generation,
            "states": list(APPROVED_EFFECT_STATES),
        },
    )
    if bound.scalar_one_or_none() is None:
        raise UncertainResolutionConflict("处置 generation 或 child 已变化")


async def _legacy_resend_candidates(
    connection: AsyncConnection, current: UncertainResolution
) -> Any:
    """旧命名空间/旧指针只用于发现待核验候选，绝不作为内部创建的证明。"""

    return (
        (
            await connection.execute(
                text("""
        SELECT b.id AS child_batch_id,b.app_id,b.dept,b.category,b.channel,b.creator,
          i.biz_id, COALESCE(c.generation,:generation) AS generation,
          EXISTS (SELECT 1 FROM uncertain_resend_creation_evidence a
            WHERE a.object_id=trim(b.batch_no) AND a.actor=:actor) AS system_audit
        FROM sms_batch b
        LEFT JOIN idempotency_record i ON i.batch_id=b.id
        LEFT JOIN sms_uncertain_child c ON c.child_batch_id=b.id
        WHERE (i.scope_kind='uncertain-resend' AND i.scope_id=:scope_id)
           OR c.resolution_id=:id OR b.id=CAST(:child_id AS bigint)
    """),
                {
                    "id": current.id,
                    "scope_id": str(current.id),
                    "generation": current.effect_generation,
                    "child_id": current.child_batch_id,
                    "actor": _effect_principal(current).actor_name,
                },
            )
        )
        .mappings()
        .all()
    )


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
    if isinstance(exc, SourceMessageStateChanged):
        return "source_message_state_changed"
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


async def _lock_not_accepted(
    connection: AsyncConnection, resolution_id: int, chunk_id: int, batch_id: int, generation: int,
) -> Any:
    """按 chunk→batch→resolution 锁定本次批准，拒绝错配及旧代际。"""
    await _lock_resolution_source(connection, resolution_id)
    row = (await connection.execute(text("""
        SELECT r.*,c.status chunk_status,c.batch_id chunk_batch_id,
          b.app_id actual_app_id,b.dept actual_dept,b.channel actual_channel,
          b.category actual_category,b.usage_reservation_id,b.segments,
          u.state usage_state,u.app_id usage_app_id,u.dept usage_dept,u.category usage_category
        FROM sms_uncertain_resolution r JOIN sms_chunk c ON c.id=r.chunk_id
        JOIN sms_batch b ON b.id=r.batch_id
        LEFT JOIN usage_reservation u ON u.id=b.usage_reservation_id
        WHERE r.id=:id
    """), {"id": resolution_id})).mappings().one_or_none()
    if row is None or (
        row["effect_generation"] != generation or row["chunk_id"] != chunk_id
        or row["batch_id"] != batch_id or row["chunk_batch_id"] != batch_id
        or row["action"] != "confirm_not_accepted"
        or row["state"] not in APPROVED_EFFECT_STATES | {"closed", "effect_applied"}
        or row["chunk_status"] != "unknown_terminal"
        or row["approved_at"] is None or row["confirmed_at"] is None
        or row["confirmer_account_id"] is None
        or row["confirmer_account_id"] == row["proposer_account_id"]
        or row["source_app_id"] != row["actual_app_id"]
        or row["source_dept"] != row["actual_dept"]
        or row["source_channel"] != row["actual_channel"]
        or row["source_category"] != row["actual_category"]
        or row["usage_reservation_id"] is None or row["usage_state"] is None
        or row["usage_dept"] != row["actual_dept"]
        or row["usage_category"] != row["actual_category"]
        or (row["actual_app_id"] is not None and row["usage_app_id"] != row["actual_app_id"])
    ):
        raise UncertainResolutionConflict("not_accepted_source_conflict")
    return row


async def _apply_not_accepted(
    connection: AsyncConnection, *, resolution_id: int, chunk_id: int, batch_id: int,
    generation: int = 1,
) -> None:
    """确认事实和整批净释放同事务提交；批次锁后只读兄弟片，不反向锁片。"""
    current = await _lock_not_accepted(connection, resolution_id, chunk_id, batch_id, generation)
    allocation = (await connection.execute(text("""
        SELECT * FROM usage_chunk_allocation WHERE chunk_id=:chunk_id
    """), {"chunk_id": chunk_id})).mappings().one_or_none()
    if allocation is None or (
        allocation["batch_id"] != batch_id
        or allocation["reservation_id"] != current["usage_reservation_id"]
        or allocation["app_id"] != current["actual_app_id"]
    ):
        raise UncertainResolutionConflict("not_accepted_allocation_conflict")
    # 不把未知来源的旧行或其他 generation 的事实自动提升为本代批准。
    existing = (await connection.execute(text(
        "SELECT * FROM usage_chunk_release WHERE resolution_id=:id"
    ), {"id": resolution_id})).mappings().one_or_none()
    proof = {
        "resolution_id": resolution_id, "chunk_id": chunk_id,
        "reservation_id": allocation["reservation_id"], "effect_generation": generation,
        "recipient_count": allocation["recipient_count"],
        "segment_count": allocation["segment_count"], "request_count": allocation["request_count"],
        "release_event_id": f"resolution:{resolution_id}:not-accepted",
    }
    if existing is not None and any(existing[key] != value for key, value in proof.items()):
        raise UncertainResolutionConflict("not_accepted_release_proof_conflict")
    if existing is None:
        await _require_active_dual_control(connection, _row(current))
        await connection.execute(text("""
            INSERT INTO usage_chunk_release(resolution_id,chunk_id,reservation_id,
              effect_generation,recipient_count,segment_count,request_count,release_event_id)
            VALUES(:resolution_id,:chunk_id,:reservation_id,:effect_generation,
              :recipient_count,:segment_count,:request_count,:release_event_id)
        """), proof)
    if not await _all_chunks_not_accepted(connection, batch_id):
        # 历史矛盾只报告，不自动再扣费或改变待发送工作。
        if current["usage_state"] in {"released", "release_requested"}:
            raise UncertainResolutionConflict("not_accepted_usage_state_conflict")
        return
    await request_usage_release_for_batch(
        connection, batch_id=batch_id,
        event_id=f"usage:{allocation['reservation_id']}:uncertain-unused",
    )


async def _all_chunks_not_accepted(connection: AsyncConnection, batch_id: int) -> bool:
    """成本补偿只允许解释完整的终止工作；未列入白名单的状态一律暂缓。"""
    from app.services.report_projection import NO_REPORT_EVIDENCE

    # 调用方已按目标 chunk→batch 顺序持锁；这里不锁兄弟 chunk。
    await connection.execute(text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                             {"id": batch_id})
    result = await connection.execute(text(f"""
        SELECT EXISTS(SELECT 1 FROM sms_chunk WHERE batch_id=:id)
        AND EXISTS(SELECT 1 FROM sms_message WHERE batch_id=:id)
        AND NOT EXISTS (
          SELECT 1 FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id
          LEFT JOIN usage_chunk_allocation a ON a.chunk_id=c.id
          WHERE c.batch_id=:id AND (
            a.chunk_id IS NULL OR a.batch_id<>b.id
            OR a.reservation_id IS DISTINCT FROM b.usage_reservation_id
            OR a.app_id IS DISTINCT FROM b.app_id
            OR a.recipient_count<>(SELECT count(*) FROM sms_message m
                                  WHERE m.chunk_id=c.id AND m.batch_id=b.id)
            OR a.segment_count<>a.recipient_count*b.segments
            OR c.status NOT IN ('failed','unknown_terminal')
            OR (c.status='unknown_terminal' AND NOT EXISTS (
              SELECT 1 FROM usage_chunk_release f JOIN sms_uncertain_resolution r
                ON r.id=f.resolution_id
              WHERE f.chunk_id=c.id AND r.chunk_id=c.id AND r.batch_id=b.id
                AND f.effect_generation=r.effect_generation
                AND f.reservation_id=a.reservation_id
                AND f.recipient_count=a.recipient_count AND f.segment_count=a.segment_count
                AND f.request_count=a.request_count
                AND f.release_event_id=concat('resolution',chr(58),r.id,chr(58),'not-accepted')
                AND r.action='confirm_not_accepted'
                AND r.state IN ('approved','effect_pending','applying','retryable_effect_error',
                                'effect_applied','closed')
                AND r.approved_at IS NOT NULL AND r.confirmed_at IS NOT NULL
                AND r.proposer_account_id<>r.confirmer_account_id
                AND r.source_app_id IS NOT DISTINCT FROM b.app_id
                AND r.source_dept=b.dept AND r.source_channel=b.channel
                AND r.source_category=b.category
            ))
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM sms_message m LEFT JOIN sms_chunk c ON c.id=m.chunk_id
          WHERE m.batch_id=:id AND (
            c.id IS NULL OR c.batch_id<>m.batch_id
            OR m.status NOT IN ('failed','unknown') OR NOT ({NO_REPORT_EVIDENCE})
            OR (m.status='unknown' AND c.status<>'unknown_terminal')
            OR (m.status='failed' AND c.status<>'failed')
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM report_event_projection p
          WHERE p.batch_id=:id AND p.projection_changed
        )
    """), {"id": batch_id})
    return bool(result.scalar_one())


async def reevaluate_confirmed_unused_batch(connection: AsyncConnection, batch_id: int) -> bool:
    """确定性失败终结后重新结算已有人工确认；调用方已按 chunk→batch 持锁。"""
    # 只选择仍为 unknown_terminal 的已确认事实，不扩大普通失败批次的退款范围。
    result = await connection.execute(text("""
        SELECT b.usage_reservation_id FROM sms_batch b
        WHERE b.id=:id AND b.usage_reservation_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM sms_chunk c JOIN sms_uncertain_resolution r ON r.chunk_id=c.id
          JOIN usage_chunk_release f ON f.resolution_id=r.id
          WHERE c.batch_id=b.id AND c.status='unknown_terminal'
            AND r.action='confirm_not_accepted' AND f.effect_generation=r.effect_generation
        )
    """), {"id": batch_id})
    reservation = result.scalar_one_or_none()
    if reservation is None or not await _all_chunks_not_accepted(connection, batch_id):
        return False
    return await request_usage_release_for_batch(
        connection, batch_id=batch_id, event_id=f"usage:{reservation}:uncertain-unused",
    )


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
