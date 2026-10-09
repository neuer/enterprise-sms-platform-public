"""受理的落库与收尾：组装批次、COMMIT 结果不明时按 reservation 事实复用或补偿、
命中幂等批次时释放本次预留，否则绑定在途预留、记住幂等结果并入队。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from app.core.auth.accounts import (
    ApplicationPrincipal,
    SecurityPrincipal,
    UncertainEffectPrincipal,
)
from app.services.approval import requires_approval
from app.services.category import CategoryPolicy
from app.services.crypto import EncryptionContext, ProtectedPhone
from app.services.pipeline_acceptance import (
    LOGGER,
    AcceptanceStagesMixin,
    PreparedContent,
    _Acceptance,
    _QuotaHold,
    _ScreenedRecipients,
)
from app.services.pipeline_contracts import (
    AcceptCommitConflict,
    AcceptCommitUnknown,
    BatchCommand,
    BatchResponse,
    StoredBatch,
)


@dataclass(frozen=True, slots=True)
class _PersistedBatch:
    stored: StoredBatch
    status: Literal["queued", "scheduled", "pending_approval"]
    deferred_reason: str | None
    scheduled_at: datetime | None


class AcceptanceCommitMixin(AcceptanceStagesMixin):
    """SendPipeline 受理的落库与收尾阶段。"""

    async def _compensate_reservation(self, acceptance: _Acceptance, hold: _QuotaHold) -> None:
        if self.usage_ledger is not None:
            await self._release_usage(acceptance, "acceptance-failed")
            return
        app = acceptance.app
        request = acceptance.request
        try:
            if hold.reservation_key is not None:
                await self.quota.refund_reservation(
                    app_id=self._usage_app_id(app, request),
                    dept=self._usage_dept(app, request),
                    category=request.category,
                    date_key=hold.date_key,
                    cost=hold.cost,
                    reservation_key=hold.reservation_key,
                )
            else:
                await self.quota.refund(
                    app_id=self._usage_app_id(app, request),
                    dept=self._usage_dept(app, request),
                    category=request.category,
                    date_key=hold.date_key,
                    cost=hold.cost,
                )
        except Exception as exc:
            LOGGER.error(
                "quota reservation compensation unavailable",
                extra={"app_id": app.app_id, "error_type": type(exc).__name__},
            )

    async def _persist_batch(
        self,
        acceptance: _Acceptance,
        prepared: PreparedContent,
        sign_name: str | None,
        screened: _ScreenedRecipients,
        accepted: Sequence[ProtectedPhone],
        hold: _QuotaHold,
    ) -> _PersistedBatch | BatchResponse:
        """组装批次并落库；COMMIT 结果不明时按 reservation 事实决定复用、冲突或补偿。

        返回 BatchResponse 表示已确认同请求的既有批次，调用方直接返回。
        """

        app = acceptance.app
        request = acceptance.request
        idem_scope = acceptance.idem_scope
        save_attempted = False
        try:
            await acceptance.check_ownership()
            scheduled_status, deferred_reason, scheduled_at = self._schedule(request)
            status: Literal["queued", "scheduled", "pending_approval"] = scheduled_status
            if not request.is_test and requires_approval(
                request.channel,
                request.category,
                len(accepted),
                notice_threshold=self.config.approval_threshold,
                market_threshold=self.config.market_approval_threshold,
            ):
                if not isinstance(request.actor, SecurityPrincipal):
                    raise ValueError("Web 审批申请人不能为空")
                status = "pending_approval"
            approval_threshold = (
                self.config.market_approval_threshold
                if status == "pending_approval" and request.category == "market"
                else self.config.approval_threshold
                if status == "pending_approval"
                else None
            )
            principal = request.actor
            if principal is None and request.channel == "api":
                principal = ApplicationPrincipal(app.app_id, app.name, app.dept)
            if request.channel == "web" and not isinstance(
                principal,
                (SecurityPrincipal, UncertainEffectPrincipal),
            ):
                raise ValueError("Web 发送必须绑定稳定账号与身份")
            if isinstance(principal, UncertainEffectPrincipal):
                verifier = getattr(self.store, "verify_uncertain_effect", None)
                if verifier is None:
                    raise ValueError("system resend principal is not forgeable")
                await verifier(principal)
            if request.import_reservation_id is not None and (
                request.channel != "web" or not isinstance(principal, SecurityPrincipal)
            ):
                raise ValueError("导入包预留只能绑定 Web 稳定主体")
            if not isinstance(
                principal,
                (SecurityPrincipal, ApplicationPrincipal, UncertainEffectPrincipal),
            ):
                raise ValueError("发送请求必须绑定稳定主体")
            batch_no = uuid4().hex
            claim_token = acceptance.claim_token
            command = BatchCommand(
                batch_no=batch_no,
                app_id=(
                    self._usage_app_id(app, request)
                    if request.channel != "web"
                    or request.vendor_test_uat
                    or self._is_system_resend(request)
                    else None
                ),
                dept=self._usage_dept(app, request),
                category=request.category,
                channel=request.channel,
                display_content_enc=self.crypto.encrypt_bound_packed_text(
                    prepared.persisted_content,
                    EncryptionContext(
                        domain="sms-display-content",
                        table="sms_batch",
                        column="display_content_enc",
                        object_id=batch_no,
                    ),
                ),
                send_content_enc=self.crypto.encrypt_bound_packed_text(
                    prepared.send_content,
                    EncryptionContext(
                        domain="sms-content",
                        table="sms_batch",
                        column="send_content_enc",
                        object_id=batch_no,
                    ),
                ),
                sign_name=sign_name,
                template_id=request.template_id,
                biz_id=request.biz_id,
                scope_kind=idem_scope.kind if idem_scope is not None else "app",
                scope_id=idem_scope.id if idem_scope is not None else "",
                segments=prepared.segments,
                quota_cost=hold.cost,
                status=status,
                deferred_reason=deferred_reason,
                scheduled_at=scheduled_at,
                request_hash=acceptance.request_hash,
                request_hash_key_version=acceptance.request_hash_key_version,
                removed_duplicate=screened.removed_duplicate,
                removed_blacklist=screened.removed_blacklist,
                removed_freq=len(screened.after_blacklist) - len(accepted),
                principal=principal,
                approval_expire_hours=self.config.approval_expire_hours,
                approval_threshold=approval_threshold,
                is_test=request.is_test,
                consent_confirmed=request.consent_confirmed,
                remark=request.remark,
                resend_of=request.resend_of,
                failed_sources=request.failed_sources,
                usage_reservation_id=acceptance.usage_reservation_id,
                import_reservation_id=request.import_reservation_id,
                inflight_reservation_id=getattr(acceptance.inflight, "id", None),
                inflight_reservation_generation=getattr(acceptance.inflight, "generation", None),
                idempotency_claim_token=(claim_token.split(":", 1)[0] if claim_token else None),
                idempotency_claim_generation=acceptance.claim_generation,
                messages=tuple(accepted),
                uncertain_source_proof=request.uncertain_source_proof,
            )
            await acceptance.check_ownership()
            save_attempted = True
            stored = await self.store.save(command)
        except Exception as original:
            if save_attempted:
                resolution = await self._resolve_acceptance_commit(
                    app=app,
                    request=request,
                    command=command,
                    idem_scope=idem_scope,
                    inflight=acceptance.inflight,
                    preauthorization=acceptance.preauthorization,
                )
                if resolution.kind == "BOUND_TO_EXPECTED_BATCH":
                    acceptance.inflight_bound = True
                    acceptance.allow_unbound_release = False
                    if not acceptance.usage_reservation_reused:
                        await self._release_usage(acceptance, "idempotent-reuse")
                    return await self.store.response_for(resolution.batch_no or command.batch_no)
                if resolution.kind == "BOUND_TO_CONFLICTING_BATCH":
                    acceptance.allow_unbound_release = False
                    raise AcceptCommitConflict(
                        "在途预留已绑定不一致批次，禁止释放或重发"
                    ) from original
                if resolution.kind == "UNKNOWN":
                    acceptance.allow_unbound_release = False
                    raise AcceptCommitUnknown(
                        "受理提交结果尚未确认，请查询原批次状态后重试"
                    ) from original
                if request.biz_id:
                    assert idem_scope is not None
                    try:
                        existing = await self.idempotency.lookup(
                            idem_scope,
                            request.biz_id,
                        )
                    except Exception:
                        await self._compensate_reservation(acceptance, hold)
                        raise original from None
                    if existing is not None:
                        try:
                            policy = self._resolve_policy(
                                app,
                                request,
                                acceptance.preauthorization,
                            )
                            await self._ensure_same_request(
                                idem_scope,
                                request.biz_id,
                                request,
                                app,
                                policy,
                            )
                        except Exception:
                            await self._compensate_reservation(acceptance, hold)
                            raise
                        if not acceptance.usage_reservation_reused:
                            await self._release_usage(acceptance, "idempotent-reuse")
                        return await self.store.response_for(existing)
            await self._compensate_reservation(acceptance, hold)
            raise
        return _PersistedBatch(
            stored=stored,
            status=status,
            deferred_reason=deferred_reason,
            scheduled_at=scheduled_at,
        )

    async def _finish_acceptance(
        self,
        acceptance: _Acceptance,
        policy: CategoryPolicy,
        prepared: PreparedContent,
        screened: _ScreenedRecipients,
        accepted: Sequence[ProtectedPhone],
        hold: _QuotaHold,
        persisted: _PersistedBatch,
    ) -> BatchResponse:
        """落库命中幂等则复用并释放本次预留；否则绑定在途预留、记住幂等结果并入队。"""

        app = acceptance.app
        request = acceptance.request
        idem_scope = acceptance.idem_scope
        stored = persisted.stored
        if stored.idempotent:
            try:
                if request.biz_id:
                    assert idem_scope is not None
                    policy = self._resolve_policy(app, request, acceptance.preauthorization)
                    await self._ensure_same_request(
                        idem_scope,
                        request.biz_id,
                        request,
                        app,
                        policy,
                    )
                return await self.store.response_for(stored.batch_no)
            finally:
                if self.usage_ledger is not None:
                    if not acceptance.usage_reservation_reused:
                        await self._release_usage(acceptance, "idempotent-reuse")
                elif not hold.reused:
                    await self._compensate_reservation(acceptance, hold)
        acceptance.inflight_bound = True
        acceptance.allow_unbound_release = False
        if request.biz_id:
            assert idem_scope is not None
            await self.idempotency.remember(idem_scope, request.biz_id, stored.batch_no)
        if persisted.status == "queued" and not stored.outbox_persisted:
            await acceptance.check_ownership()
            await self.publisher.enqueue(stored.batch_no, policy.queue)
        expires_at = None
        if request.biz_id:
            expires_at = (
                persisted.scheduled_at + timedelta(days=7)
                if persisted.scheduled_at is not None
                else self.clock() + timedelta(hours=24)
            )
        return BatchResponse(
            stored.batch_no,
            False,
            len(accepted),
            screened.removed_duplicate,
            screened.removed_blacklist,
            len(screened.after_blacklist) - len(accepted),
            prepared.segments,
            hold.cost,
            persisted.status,
            persisted.deferred_reason,
            persisted.scheduled_at,
            expires_at,
        )
