"""发送受理的幂等：请求指纹、稳定作用域、同请求校验与 COMMIT 边界解析。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import (
    ApplicationPrincipal,
    SecurityPrincipal,
    UncertainEffectPrincipal,
)
from app.services.category import CategoryPolicy
from app.services.crypto import CryptoService
from app.services.idempotency import (
    IdempotencyConflict,
    IdempotencyFingerprint,
    IdempotencyScope,
    uncertain_resend_biz_id,
)
from app.services.pipeline_admission import SendAdmissionMixin
from app.services.pipeline_contracts import (
    AcceptancePreauthorization,
    BatchCommand,
    IdempotencyPort,
    SendRequest,
)

# 与入口共用日志器名，日志路由与告警过滤保持不变。
LOGGER = logging.getLogger("app.services.pipeline")
CLAIM_CLEANUP_TIMEOUT_S = 2.0


def _same_digest(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _canonical_scheduled_at(value: datetime | None) -> str | None:
    """把定时时刻归一为 UTC 瞬时，避免 +08:00 / Z 两种写法打出不同指纹。"""

    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AcceptanceIdempotencyMixin(SendAdmissionMixin):
    """SendPipeline 的幂等步骤；指纹绑定类别策略，因此建立在准入 mixin 之上。"""

    crypto: CryptoService
    idempotency: IdempotencyPort

    def _request_hash(
        self,
        request: SendRequest,
        app: ApiAppContext,
        policy: CategoryPolicy,
        *,
        key_version: int | None = None,
        normalize: bool = True,
    ) -> str:
        """生成版本化请求 HMAC，覆盖会改变真实短信副作用与作用域的字段。"""

        actor = request.actor
        actor_document: dict[str, object] | None
        if isinstance(actor, SecurityPrincipal):
            actor_document = {
                "kind": "human",
                "account_id": actor.account_id,
                "identity_id": actor.identity_id,
            }
        elif isinstance(actor, ApplicationPrincipal):
            actor_document = {"kind": "app", "app_id": actor.app_id}
        elif isinstance(actor, UncertainEffectPrincipal):
            actor_document = {
                "kind": "uncertain-effect",
                "resolution_id": actor.resolution_id,
                "proposer_account_id": actor.proposer_account_id,
                "confirmer_account_id": actor.confirmer_account_id,
                "effect_generation": actor.effect_generation,
            }
        else:
            actor_document = None
        fingerprint_key_version = self.crypto.active_version if key_version is None else key_version
        protected_aliases = dict(request.protected_hmac_candidates)
        protected_identity: dict[str, object] | None = None
        if request.protected_mobiles:
            try:
                protected_digest = protected_aliases[fingerprint_key_version]
            except KeyError:
                raise ValueError("加密测试号码缺少幂等指纹版本") from None
            protected_identity = {
                "key_version": fingerprint_key_version,
                "digest": protected_digest,
            }
        document = {
            "app_id": app.app_id,
            "dept": app.dept,
            "actor": actor_document,
            "channel": request.channel,
            "category": request.category,
            "content": request.content,
            "template_id": request.template_id,
            "template_params": list(request.template_params or ()),
            "sign_name": request.sign_name or app.default_sign,
            "scheduled_at": _canonical_scheduled_at(request.scheduled_at)
            if normalize
            else (request.scheduled_at.isoformat() if request.scheduled_at is not None else None),
            "consent_confirmed": request.consent_confirmed,
            "is_test": request.is_test,
            "mobiles": (
                sorted(set(request.mobiles or ())) if normalize else list(request.mobiles or ())
            ),
            "protected_phone_identity": protected_identity,
            "vendor_test_uat": request.vendor_test_uat,
            "resend_of": request.resend_of,
            "resend_dept": request.resend_dept,
            "usage_subject": (
                request.usage_subject.fingerprint() if request.usage_subject is not None else None
            ),
            "policy": {
                "queue": policy.queue,
                "blacklist_required": policy.blacklist_required,
            },
        }
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self.crypto.idempotency_fingerprint(
            canonical,
            key_version=key_version,
        )

    async def acceptance_fingerprint(
        self, app: ApiAppContext, request: SendRequest
    ) -> tuple[str, int]:
        """提供与受理幂等记录一致的版本化指纹，供受控恢复绑定。"""

        version = self.crypto.active_version
        if request.biz_id:
            scope = self._idempotency_scope(request, app)
            stored = await self.idempotency.request_fingerprint(scope, request.biz_id)
            if stored is not None:
                version = stored.key_version
        policy = self._resolve_policy(app, request, None)
        return self._request_hash(request, app, policy, key_version=version), version

    @staticmethod
    def _idempotency_scope(
        request: SendRequest,
        app: ApiAppContext,
    ) -> IdempotencyScope:
        """稳定幂等主体：API=app，Web=稳定账号/身份复合作用域。"""

        if isinstance(request.actor, UncertainEffectPrincipal):
            actor = request.actor
            if request.biz_id != uncertain_resend_biz_id(
                actor.resolution_id, actor.effect_generation
            ):
                raise ValueError("system resend principal is not forgeable")
            if request.resend_of is not None or request.usage_subject is None:
                raise ValueError("system resend requires its own usage subject")
            if request.usage_subject.app_id != app.app_id:
                raise ValueError("system resend usage app mismatch")
            return IdempotencyScope("uncertain-resend", str(actor.resolution_id))
        if request.resend_of is not None:
            web_actor = isinstance(request.actor, SecurityPrincipal) and request.channel == "web"
            api_actor = (
                isinstance(request.actor, ApplicationPrincipal)
                and request.channel == "api"
                and request.actor.app_id == app.app_id
                and app.app_id > 0
            )
            if not (web_actor or api_actor):
                raise ValueError("失败重发必须绑定稳定授权主体")
            return IdempotencyScope("resend", request.resend_of)
        if request.channel == "web":
            if isinstance(request.actor, UncertainEffectPrincipal):
                raise ValueError("system resend principal is not forgeable")
            if not isinstance(request.actor, SecurityPrincipal):
                raise ValueError("Web 发送必须绑定稳定账号")
            return IdempotencyScope(
                "account",
                f"{request.actor.account_id}:{request.actor.identity_id}",
            )
        return IdempotencyScope("app", str(app.app_id))

    async def _claim_owner(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        fingerprint: str,
    ) -> str | None:
        try:
            return await self.idempotency.claim(
                scope,
                biz_id,
                fingerprint=fingerprint,
            )
        except TypeError:
            return await self.idempotency.claim(scope, biz_id)

    async def _ensure_same_request(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        request: SendRequest,
        app: ApiAppContext,
        policy: CategoryPolicy,
        *,
        computed: IdempotencyFingerprint | None = None,
    ) -> None:
        """用记录绑定的 HMAC 版本复算；旧记录无指纹时沿用原幂等行为。"""

        stored = await self.idempotency.request_fingerprint(scope, biz_id)
        if stored is None:
            raise IdempotencyConflict("同一幂等键缺少请求指纹，拒绝复用，请更换 biz_id")
        try:
            request_hash = (
                computed.digest
                if computed is not None and computed.key_version == stored.key_version
                else self._request_hash(request, app, policy, key_version=stored.key_version)
            )
            if _same_digest(stored.digest, request_hash):
                return
            legacy_hash = self._request_hash(
                request,
                app,
                policy,
                key_version=stored.key_version,
                normalize=False,
            )
        except ValueError:
            # 记录绑定的 HMAC 版本已在轮换中退役：无法证明是同一请求。
            # 若按 400 参数错误返回，调用方最自然的反应是换 biz_id 重发，
            # 恰好击穿幂等要防的重复下发；因此按幂等冲突 409 处理。
            raise IdempotencyConflict(
                "同一幂等键的请求指纹版本已退役，无法验证同请求；"
                "请先查询原批次状态，勿直接更换 biz_id 重发"
            ) from None
        if _same_digest(stored.digest, legacy_hash):
            return
        raise IdempotencyConflict("同一幂等键已用于不同请求，请更换 biz_id 或复用原请求")

    async def _resolve_acceptance_commit(
        self,
        *,
        app: ApiAppContext,
        request: SendRequest,
        command: BatchCommand,
        idem_scope: IdempotencyScope | None,
        inflight: Any,
        preauthorization: AcceptancePreauthorization | None,
    ) -> Any:
        """用数据库 reservation 事实解析 COMMIT 边界；无解析器时回退查找。"""

        from app.services.send_inflight import AcceptCommitResolution

        resolver = getattr(self.store, "resolve_ambiguous_acceptance_commit", None)
        reservation_id = getattr(inflight, "id", None)
        generation = getattr(inflight, "generation", None)
        if resolver is not None and reservation_id is not None and generation is not None:
            scope = idem_scope or IdempotencyScope(
                command.scope_kind,
                command.scope_id,
            )
            try:
                return await resolver(
                    reservation_id=int(reservation_id),
                    generation=int(generation),
                    app_id=app.app_id,
                    scope_kind=scope.kind,
                    scope_id=scope.id,
                    biz_id=command.biz_id or "",
                    request_hash=command.request_hash or "",
                )
            except Exception:
                return AcceptCommitResolution("UNKNOWN")
        if request.biz_id and idem_scope is not None:
            try:
                existing = await self.idempotency.lookup(idem_scope, request.biz_id)
            except Exception:
                if reservation_id is None:
                    return AcceptCommitResolution("UNBOUND")
                return AcceptCommitResolution("UNKNOWN")
            if existing is not None:
                try:
                    policy = self._resolve_policy(app, request, preauthorization)
                    await self._ensure_same_request(
                        idem_scope,
                        request.biz_id,
                        request,
                        app,
                        policy,
                    )
                except IdempotencyConflict:
                    return AcceptCommitResolution("UNBOUND")
                return AcceptCommitResolution(
                    "BOUND_TO_EXPECTED_BATCH",
                    batch_no=existing,
                )
        return AcceptCommitResolution("UNBOUND")

    def _with_uat_replay_identity(self, request: SendRequest) -> SendRequest:
        """用内存 HMAC 复算 UAT 指纹，不依赖登记号码仍为 active。"""

        if not request.vendor_test_uat or request.protected_mobiles or not request.mobiles:
            return request
        phone = request.mobiles[0]
        protected = self.crypto.protect_phone(
            phone,
            table="vendor_test_recipient",
            column="phone_enc",
        )
        return replace(
            request,
            mobiles=(),
            protected_mobiles=(protected,),
            protected_hmac_candidates=tuple(self.crypto.hmac_candidates(phone).items()),
        )

    async def _cleanup_claim(
        self,
        heartbeat: asyncio.Task[None] | None,
        scope: IdempotencyScope,
        biz_id: str,
        token: str,
        app_id: int,
    ) -> None:
        """先有界停止自己的续租，再独立尝试权威 CAS 释放；保留业务异常。"""

        if heartbeat is not None:
            heartbeat.cancel()
            try:
                async with asyncio.timeout(CLAIM_CLEANUP_TIMEOUT_S):
                    await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOGGER.error(
                    "idempotency heartbeat stop unavailable",
                    extra={"app_id": app_id, "error_type": type(exc).__name__},
                )
        try:
            async with asyncio.timeout(CLAIM_CLEANUP_TIMEOUT_S):
                await self.idempotency.release(scope, biz_id, token)
        except (Exception, asyncio.CancelledError) as exc:
            LOGGER.error(
                "idempotency claim release unavailable",
                extra={"app_id": app_id, "error_type": type(exc).__name__},
            )
