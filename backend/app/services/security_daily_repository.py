"""安全日报事实表与投递请求的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import database_engine
from app.services.security_daily import (
    MAX_RESEND_RECIPIENTS,
    SHANGHAI_TZ,
    DeliveryAction,
    DeliveryRequestState,
    DeliveryStatus,
    GenerationSource,
    GenerationStatus,
    SecurityDailyAuditEvent,
    SecurityDailyAuditEvidence,
    SecurityDailyConfiguration,
    SecurityDailyConfigurationError,
    SecurityDailyConfigurationUpdate,
    SecurityDailyControlResult,
    SecurityDailyDeliveryRequest,
    SecurityDailyOverview,
    SecurityDailyPage,
    SecurityDailyQuery,
    SecurityDailyReportRecord,
    SecurityDailyRepository,
    SecurityDailyStateConflict,
    SecurityDailyValidationError,
    SecurityStatus,
    _next_schedule,
    resolve_configuration_state,
    validate_resend_api_key,
    validate_resend_recipients,
    validate_security_daily_payload,
)
from app.settings import Settings, get_settings

SENDER_DOMAIN = "reports.neuer.cn"
SENDER_ADDRESS = "security-daily@reports.neuer.cn"


def _bool_config(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SecurityDailyConfigurationError("安全日报布尔配置无效")


def _record(row: Any, *, include_payload: bool) -> SecurityDailyReportRecord:
    payload: dict[str, Any] | None = None
    generation_status = cast(GenerationStatus, str(row["generation_status"]))
    if include_payload and row["payload"] is not None:
        try:
            raw = cast(dict[str, Any], row["payload"])
            payload = validate_security_daily_payload(raw)
        except (TypeError, SecurityDailyValidationError):
            generation_status = "unavailable"
            payload = None
    return SecurityDailyReportRecord(
        id=int(row["id"]),
        report_date=row["report_date"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        status=cast(SecurityStatus, str(row["status"])),
        generation_source=cast(GenerationSource, str(row["generation_source"])),
        generation_status=generation_status,
        delivery_status=cast(DeliveryStatus, str(row["delivery_status"])),
        generated_at=row["generated_at"],
        delivered_at=row["delivered_at"],
        recipient_count=int(row["recipient_count"]),
        retry_count=int(row["retry_count"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        last_error_at=row["last_error_at"],
        updated_at=row["updated_at"],
        payload=payload,
    )


class SqlSecurityDailyRepository(SecurityDailyRepository):
    """安全日报查询与投递请求写入均使用 API 的 accept 数据库身份。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def generation_config(self) -> tuple[bool, int]:
        """读取日报生成开关与 UI 配置的收件人数。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                enabled_result = await connection.execute(
                    text(
                        "SELECT value FROM sys_config WHERE key='security_daily_enabled'"
                    )
                )
                enabled_row = enabled_result.mappings().one_or_none()
                count_result = await connection.execute(
                    text("SELECT count(*) FROM security_daily_recipient")
                )
                count = int(count_result.scalar_one())
                if count > MAX_RESEND_RECIPIENTS:
                    raise SecurityDailyConfigurationError("安全日报收件人数量超出范围")
                return _bool_config(str(enabled_row["value"]) if enabled_row else None), count
        finally:
            await engine.dispose()

    async def configuration(self) -> SecurityDailyConfiguration:
        """读取安全日报 UI 配置；缺少新 Key 时按未配置处理。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                config_result = await connection.execute(
                    text(
                        "SELECT key,value FROM sys_config WHERE key IN "
                        "('security_daily_enabled','security_daily_resend_api_key')"
                    )
                )
                config = {str(row["key"]): str(row["value"]) for row in config_result.mappings()}
                recipients_result = await connection.execute(
                    text(
                        "SELECT position,address FROM security_daily_recipient "
                        "ORDER BY position"
                    )
                )
                recipients = tuple(str(row["address"]) for row in recipients_result.mappings())
        finally:
            await engine.dispose()
        return SecurityDailyConfiguration(
            enabled=_bool_config(config.get("security_daily_enabled")),
            api_key=validate_resend_api_key(config.get("security_daily_resend_api_key", "")),
            recipients=validate_resend_recipients(recipients),
        )

    async def audit_evidence(
        self, period_start: datetime, period_end: datetime
    ) -> SecurityDailyAuditEvidence | None:
        """读取只读审计证据视图，返回最近事件与总数；视图不可用时返回 None。

        只查询视图暴露的非载荷列，绝不读取 before/after 审计明细。
        """

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                events_result = await connection.execute(
                    text(
                        """
                        SELECT created_at, actor, source_ip, action
                        FROM security_daily_audit_evidence
                        WHERE created_at >= :start AND created_at < :end
                          AND action NOT LIKE 'security_daily_%'
                        ORDER BY created_at DESC
                        LIMIT 10
                        """
                    ),
                    {"start": period_start, "end": period_end},
                )
                count_result = await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM security_daily_audit_evidence
                        WHERE created_at >= :start AND created_at < :end
                        """
                    ),
                    {"start": period_start, "end": period_end},
                )
        except SQLAlchemyError:
            return None
        finally:
            await engine.dispose()
        events = tuple(
            SecurityDailyAuditEvent(
                time=row["created_at"].astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                actor=str(row["actor"]),
                source_ip=str(row["source_ip"]),
                action=str(row["action"]),
            )
            for row in events_result.mappings()
        )
        return SecurityDailyAuditEvidence(total=int(count_result.scalar_one()), events=events)

    async def update_configuration(
        self,
        update: SecurityDailyConfigurationUpdate,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyConfiguration:
        """事务性保存安全日报开关、Resend Key 和收件人，并写入审计。"""

        recipients = validate_resend_recipients(update.recipients)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text("SELECT pg_advisory_xact_lock(83174621)"))
                current_result = await connection.execute(
                    text(
                        "SELECT value FROM sys_config "
                        "WHERE key='security_daily_resend_api_key'"
                    )
                )
                current_row = current_result.mappings().one_or_none()
                current_key = str(current_row["value"]) if current_row is not None else ""
                api_key = (
                    validate_resend_api_key(update.api_key)
                    if update.api_key is not None
                    else validate_resend_api_key(current_key)
                )
                values = {
                    "security_daily_enabled": "true" if update.enabled else "false",
                    "security_daily_resend_api_key": api_key,
                    "security_daily_recipient_count": str(len(recipients)),
                    "security_daily_resend_configured": "true" if api_key else "false",
                }
                for key, value in values.items():
                    await connection.execute(
                        text(
                            "UPDATE sys_config SET value=:value,updated_by=:actor,"
                            "updated_at=now() WHERE key=:key"
                        ),
                        {"key": key, "value": value, "actor": principal.login_name},
                    )
                await connection.execute(text("DELETE FROM security_daily_recipient"))
                for position, address in enumerate(recipients, start=1):
                    await connection.execute(
                        text(
                            "INSERT INTO security_daily_recipient(position,address) "
                            "VALUES(:position,:address)"
                        ),
                        {"position": position, "address": address},
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          'admin',CAST(:ip AS inet),'security_daily_config_update',
                          'security_daily_config','default',CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "actor": principal.login_name,
                        "actor_account_id": principal.account_id,
                        "actor_identity_id": principal.identity_id,
                        "ip": ip,
                        "after": json.dumps(
                            {
                                "enabled": update.enabled,
                                "resend_configured": bool(api_key),
                                "recipient_count": len(recipients),
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                return SecurityDailyConfiguration(update.enabled, api_key, recipients)
        finally:
            await engine.dispose()

    async def ingest_payload(
        self,
        payload: dict[str, Any],
        *,
        recipient_count: int,
        force: bool = False,
        generation_source: GenerationSource = "auto",
    ) -> bool:
        """把外部证据采集器交付的脱敏日报原子写入事实表。"""

        validated = validate_security_daily_payload(payload)
        report_date = date.fromisoformat(str(validated["report_date"]))
        period_start = datetime.fromisoformat(str(validated["period_start"]))
        period_end = datetime.fromisoformat(str(validated["period_end"]))
        generated_at = datetime.fromisoformat(str(validated["generated_at"]))
        conflict_update = """
            SET period_start=EXCLUDED.period_start,
                period_end=EXCLUDED.period_end,
                status=EXCLUDED.status,
                generation_status='ready',
                generation_source=EXCLUDED.generation_source,
                payload=EXCLUDED.payload,
                generated_at=EXCLUDED.generated_at,
                recipient_count=EXCLUDED.recipient_count,
                last_error=NULL,last_error_at=NULL,updated_at=now()
        """
        conflict_where = """
            WHERE security_daily_report.generation_status <> 'ready'
              AND security_daily_report.last_error IS DISTINCT FROM EXCLUDED.last_error
        """
        if force:
            # 手动重新生成：无条件覆盖并重置投递状态，随后由调用方重新提交发送。
            conflict_update += (
                ",delivery_status='not_sent',delivered_at=NULL,retry_count=0"
            )
            conflict_where = ""
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO security_daily_report(
                          report_date,period_start,period_end,status,generation_status,
                          generation_source,delivery_status,payload,generated_at,recipient_count,
                          last_error,last_error_at,updated_at
                        ) VALUES(
                          :report_date,:period_start,:period_end,:status,'ready',
                          :generation_source,'not_sent',CAST(:payload AS jsonb),:generated_at,
                          :recipient_count,NULL,NULL,now()
                        )
                        ON CONFLICT (report_date) DO UPDATE
                        """
                        + conflict_update
                        + conflict_where
                        + """
                        RETURNING id
                        """
                    ),
                    {
                        "report_date": report_date,
                        "period_start": period_start,
                        "period_end": period_end,
                        "status": validated["status"],
                        "generation_source": generation_source,
                        "payload": json.dumps(validated, ensure_ascii=False),
                        "generated_at": generated_at,
                        "recipient_count": min(3, max(0, recipient_count)),
                    },
                )
                if result.scalar_one_or_none() is None:
                    return False
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id,after_val
                        ) VALUES(
                          'security-report-collector','system','system',
                          'security_daily_generated','security_daily_report',
                          :object_id,CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "object_id": report_date.isoformat(),
                        "after": json.dumps(
                            {
                                "status": validated["status"],
                                "generation_status": "ready",
                                "generation_source": generation_source,
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                return True
        finally:
            await engine.dispose()

    async def mark_unavailable(
        self,
        report_date: date,
        *,
        period_start: datetime,
        period_end: datetime,
        reason: str,
        generation_source: GenerationSource = "auto",
    ) -> bool:
        """证据源缺失时落明确 unavailable，不用 0 伪造指标。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO security_daily_report(
                          report_date,period_start,period_end,status,generation_status,
                          generation_source,delivery_status,payload,last_error,last_error_at,updated_at
                        ) VALUES(
                          :report_date,:period_start,:period_end,'attention','unavailable',
                          :generation_source,'not_sent',NULL,:error,now(),now()
                        )
                        ON CONFLICT (report_date) DO UPDATE
                        SET generation_source=EXCLUDED.generation_source,
                            period_start=EXCLUDED.period_start,
                            period_end=EXCLUDED.period_end,
                            status='attention',generation_status='unavailable',
                            payload=NULL,delivery_status='not_sent',
                            last_error=EXCLUDED.last_error,last_error_at=now(),updated_at=now()
                        WHERE security_daily_report.generation_status <> 'ready'
                        RETURNING id
                        """
                    ),
                    {
                        "report_date": report_date,
                        "period_start": period_start,
                        "period_end": period_end,
                        "generation_source": generation_source,
                        "error": reason[:256],
                    },
                )
                if result.scalar_one_or_none() is None:
                    return False
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id,after_val
                        ) VALUES(
                          'security-report-collector','system','system',
                          'security_daily_generation_unavailable','security_daily_report',
                          :object_id,CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "object_id": report_date.isoformat(),
                        "after": json.dumps(
                            {
                                "generation_status": "unavailable",
                                "generation_source": generation_source,
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                return True
        finally:
            await engine.dispose()

    async def overview(self, *, now: datetime) -> SecurityDailyOverview:
        configuration = await self.configuration()
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                latest_result = await connection.execute(
                    text(
                        """
                        SELECT id,report_date,period_start,period_end,status,
                          generation_status,delivery_status,generated_at,delivered_at,
                          recipient_count,retry_count,last_error,last_error_at,updated_at,
                          NULL::jsonb AS payload
                        FROM security_daily_report
                        ORDER BY report_date DESC,id DESC LIMIT 1
                        """
                    )
                )
                latest = latest_result.mappings().one_or_none()
                generated_result = await connection.execute(
                    text(
                        "SELECT max(generated_at) FROM security_daily_report "
                        "WHERE generation_status='ready'"
                    )
                )
                delivered_result = await connection.execute(
                    text(
                        "SELECT max(delivered_at) FROM security_daily_report "
                        "WHERE delivery_status='sent'"
                    )
                )
        finally:
            await engine.dispose()
        enabled = configuration.enabled
        resend_configured = bool(configuration.api_key)
        recipient_count = len(configuration.recipients)
        return SecurityDailyOverview(
            enabled=enabled,
            configuration_state=resolve_configuration_state(
                enabled=enabled,
                resend_configured=resend_configured,
                recipient_count=recipient_count,
            ),
            schedule_time="08:00",
            timezone="Asia/Shanghai",
            period_description="汇总前一自然日（北京时间）",
            last_generated_at=generated_result.scalar_one_or_none(),
            last_delivered_at=delivered_result.scalar_one_or_none(),
            next_scheduled_at=_next_schedule(now) if enabled else None,
            latest_failure=(
                str(latest["last_error"])
                if latest is not None and latest["last_error"] is not None
                else None
            ),
            delivery_status=(
                cast(DeliveryStatus, str(latest["delivery_status"])) if latest is not None else None
            ),
            recipient_count=recipient_count,
            resend_configured=resend_configured,
            sender_domain=SENDER_DOMAIN,
            sender_address=SENDER_ADDRESS,
            beat_restart_required=True,
        )

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        predicates: list[str] = []
        params: dict[str, Any] = {
            "limit": query.page_size,
            "offset": (query.page - 1) * query.page_size,
        }
        if query.report_date_from is not None:
            predicates.append("report_date>=:date_from")
            params["date_from"] = query.report_date_from
        if query.report_date_to is not None:
            predicates.append("report_date<=:date_to")
            params["date_to"] = query.report_date_to
        if query.status is not None:
            predicates.append("status=:status")
            params["status"] = query.status
        if query.generation_status is not None:
            predicates.append("generation_status=:generation_status")
            params["generation_status"] = query.generation_status
        if query.delivery_status is not None:
            predicates.append("delivery_status=:delivery_status")
            params["delivery_status"] = query.delivery_status
        where = " AND ".join(predicates) or "TRUE"
        columns = (
            "id,report_date,period_start,period_end,status,generation_status,"
            "generation_source,delivery_status,generated_at,delivered_at,recipient_count,retry_count,"
            "last_error,last_error_at,updated_at,NULL::jsonb AS payload"
        )
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count_result = await connection.execute(
                    text(f"SELECT count(*) FROM security_daily_report WHERE {where}"),
                    params,
                )
                result = await connection.execute(
                    text(
                        f"SELECT {columns} FROM security_daily_report WHERE {where} "
                        "ORDER BY report_date DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(_record(row, include_payload=False) for row in result.mappings())
                return SecurityDailyPage(
                    items,
                    int(count_result.scalar_one()),
                    query.page,
                    query.page_size,
                )
        finally:
            await engine.dispose()

    async def get_report(self, report_date: date) -> SecurityDailyReportRecord | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,report_date,period_start,period_end,status,
                          generation_status,generation_source,delivery_status,generated_at,delivered_at,
                          recipient_count,retry_count,last_error,last_error_at,updated_at,payload
                        FROM security_daily_report WHERE report_date=:report_date
                        """
                    ),
                    {"report_date": report_date},
                )
                row = result.mappings().one_or_none()
                return _record(row, include_payload=True) if row is not None else None
        finally:
            await engine.dispose()

    async def request_delivery(
        self,
        report: SecurityDailyReportRecord,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
    ) -> SecurityDailyDeliveryRequest:
        request_id = uuid4()
        if system:
            actor = "security-report-scheduler"
            actor_kind = "system"
            role = "system"
            actor_account_id = None
            actor_identity_id = None
            audit_ip = None
            requested_by = "system"
        else:
            if principal is None or ip is None:
                raise SecurityDailyStateConflict("人工投递缺少操作主体")
            actor = principal.login_name
            actor_kind = "human"
            role = "admin"
            actor_account_id = principal.account_id
            actor_identity_id = principal.identity_id
            audit_ip = ip
            requested_by = principal.login_name
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                locked_result = await connection.execute(
                    text(
                        "SELECT delivery_status,retry_count FROM security_daily_report "
                        "WHERE id=:id FOR UPDATE"
                    ),
                    {"id": report.id},
                )
                locked = locked_result.mappings().one_or_none()
                if locked is None:
                    raise SecurityDailyStateConflict("日报已不存在")
                delivery_status = str(locked["delivery_status"])
                retry_count = int(locked["retry_count"])
                if action == "send" and delivery_status in {"pending", "sending", "sent"}:
                    existing = await connection.execute(
                        text(
                            "SELECT request_id,requested_at,state "
                            "FROM security_daily_delivery_request "
                            "WHERE report_id=:report_id ORDER BY requested_at DESC LIMIT 1"
                        ),
                        {"report_id": report.id},
                    )
                    row = existing.mappings().one_or_none()
                    if row is not None:
                        return SecurityDailyDeliveryRequest(
                            request_id=UUID(str(row["request_id"])),
                            report_date=report.report_date,
                            action="send",
                            state=cast(DeliveryRequestState, str(row["state"])),
                            requested_at=row["requested_at"],
                            idempotent=True,
                        )
                    return SecurityDailyDeliveryRequest(
                        request_id=request_id,
                        report_date=report.report_date,
                        action=action,
                        state="sent" if delivery_status == "sent" else "pending",
                            requested_at=datetime.now(SHANGHAI_TZ),
                        idempotent=True,
                    )
                if action == "retry" and delivery_status != "failed":
                    raise SecurityDailyStateConflict("只有投递失败的日报允许重试")
                next_retry = retry_count + (
                    1 if action == "retry" or delivery_status == "failed" else 0
                )
                # 每次投递请求使用独立 request_id，避免同一报告/快照重复生成时
                # 复用同一 dedup_key（唯一约束冲突会让“立即生成”变成 500）。
                dedup_key = (
                    f"security-daily:{action}:{report.report_date}:"
                    f"{next_retry}:{request_id}"
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO security_daily_delivery_request(
                          request_id,report_id,report_date,action,state,dedup_key,requested_by
                        ) VALUES(
                          :request_id,:report_id,:report_date,:action,'pending',
                          :dedup_key,:actor
                        )
                        """
                    ),
                    {
                        "request_id": request_id,
                        "report_id": report.id,
                        "report_date": report.report_date,
                        "action": action,
                        "dedup_key": dedup_key,
                        "actor": requested_by,
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE security_daily_report
                        SET delivery_status='pending',retry_count=:retry_count,
                            last_error = CASE
                              WHEN last_error LIKE '安全日报发信配置不完整%'
                              THEN NULL ELSE last_error END,
                            last_error_at = CASE
                              WHEN last_error LIKE '安全日报发信配置不完整%'
                              THEN NULL ELSE last_error_at END,
                            updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {"id": report.id, "retry_count": next_retry},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,:actor_kind,:actor_account_id,:actor_identity_id,
                          :role,CAST(:ip AS inet),:action,'security_daily_report',
                          :object_id,CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "actor": actor,
                        "actor_kind": actor_kind,
                        "actor_account_id": actor_account_id,
                        "actor_identity_id": actor_identity_id,
                        "role": role,
                        "ip": audit_ip,
                        "action": f"security_daily_{action}",
                        "object_id": report.report_date.isoformat(),
                        "after": json.dumps(
                            {"request_id": str(request_id), "status": "requested"},
                            ensure_ascii=False,
                        ),
                    },
                )
                return SecurityDailyDeliveryRequest(
                    request_id=request_id,
                    report_date=report.report_date,
                    action=action,
                    state="pending",
                    requested_at=datetime.now(SHANGHAI_TZ),
                    idempotent=False,
                )
        finally:
            await engine.dispose()

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]:
        """返回待由独立 mailer 结果文件确认的有限请求集合。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT request_id,report_date
                        FROM security_daily_delivery_request
                        WHERE state='pending'
                        ORDER BY requested_at ASC
                        LIMIT 100
                        """
                    )
                )
                return tuple(
                    (UUID(str(row["request_id"])), row["report_date"])
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None:
        """以请求锁为边界单调回写 mailer 的 sent/failed 结果并追加系统审计。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                request_result = await connection.execute(
                    text(
                        """
                        SELECT report_id,report_date,state
                        FROM security_daily_delivery_request
                        WHERE request_id=:request_id
                        FOR UPDATE
                        """
                    ),
                    {"request_id": result.request_id},
                )
                request = request_result.mappings().one_or_none()
                if request is None:
                    raise SecurityDailyStateConflict("安全日报投递请求不存在")
                if request["report_date"] != result.report_date:
                    raise SecurityDailyStateConflict("安全日报投递请求日期不匹配")
                if str(request["state"]) != "pending":
                    return
                await connection.execute(
                    text(
                        """
                        UPDATE security_daily_delivery_request
                        SET state=:state,completed_at=:completed_at,error=:error
                        WHERE request_id=:request_id
                        """
                    ),
                    {
                        "request_id": result.request_id,
                        "state": result.state,
                        "completed_at": result.completed_at,
                        "error": result.error,
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE security_daily_report
                        SET delivery_status=:delivery_status,
                            delivered_at=CASE WHEN :state='sent' THEN :completed_at
                                              ELSE delivered_at END,
                            last_error=CASE WHEN :state='failed' THEN :error ELSE NULL END,
                            last_error_at=CASE WHEN :state='failed' THEN :completed_at
                                               ELSE NULL END,
                            updated_at=now()
                        WHERE id=:report_id
                        """
                    ),
                    {
                        "report_id": request["report_id"],
                        "delivery_status": "sent" if result.state == "sent" else "failed",
                        "state": result.state,
                        "completed_at": result.completed_at,
                        "error": result.error,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id,after_val
                        ) VALUES(
                          'security-report-mailer','system','system',
                          'security_daily_delivery_result','security_daily_report',
                          :object_id,CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "object_id": result.report_date.isoformat(),
                        "after": json.dumps(
                            {"request_id": str(result.request_id), "state": result.state},
                            ensure_ascii=False,
                        ),
                    },
                )
        finally:
            await engine.dispose()

    async def mark_request_failed(self, request_id: UUID, message: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        WITH changed AS (
                          UPDATE security_daily_delivery_request
                          SET state='failed',completed_at=now(),error=:error
                          WHERE request_id=:request_id AND state='pending'
                          RETURNING report_id,report_date
                        ), updated AS (
                          UPDATE security_daily_report report
                          SET delivery_status='failed',last_error=:error,
                              last_error_at=now(),updated_at=now()
                          FROM changed
                          WHERE report.id=changed.report_id
                          RETURNING changed.report_date
                        )
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id,after_val
                        )
                        SELECT 'security-report-mailer','system','system',
                          'security_daily_delivery_result','security_daily_report',
                          report_date::text,CAST(:after AS jsonb)
                        FROM updated
                        """
                    ),
                    {
                        "request_id": request_id,
                        "error": message[:256],
                        "after": json.dumps(
                            {"request_id": str(request_id), "state": "failed"},
                            ensure_ascii=False,
                        ),
                    },
                )
        finally:
            await engine.dispose()

    async def mark_delivery_failed(self, report_date: date, message: str) -> bool:
        """投递前置条件不满足时显式标记失败，避免静默跳过。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE security_daily_report
                        SET delivery_status='failed',last_error=:error,
                            last_error_at=now(),updated_at=now()
                        WHERE report_date=:report_date
                        RETURNING id
                        """
                    ),
                    {"report_date": report_date, "error": message[:256]},
                )
                return result.scalar_one_or_none() is not None
        finally:
            await engine.dispose()
