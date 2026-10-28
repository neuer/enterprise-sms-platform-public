"""安全日报生成与投递的编排服务。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.admin_authorization import AdminAuthorization
from app.core.query_bounds import ensure_page_offset
from app.services.security_daily.contract import (
    MAX_SECURITY_DAILY_INPUT_BYTES,
    SHANGHAI_TZ,
    DeliveryAction,
    GenerationSource,
    SecurityDailyAuditEvidence,
    SecurityDailyAutoDeliveryConfiguration,
    SecurityDailyConfiguration,
    SecurityDailyConfigurationUpdate,
    SecurityDailyDeliveryRequest,
    SecurityDailyNotFound,
    SecurityDailyOverview,
    SecurityDailyPage,
    SecurityDailyPayloadData,
    SecurityDailyPreview,
    SecurityDailyQuery,
    SecurityDailyReportRecord,
    SecurityDailyStateConflict,
    SecurityDailyTimelineEvent,
    SecurityDailyUnavailable,
    SecurityDailyValidationError,
    recipient_set_digest,
    validate_security_daily_payload,
)
from app.services.security_daily.control import (
    SecurityDailyConfigurationSuperseded,
    SecurityDailyControl,
    SecurityDailyControlError,
    SecurityDailyControlResult,
)
from app.services.security_daily.enrich import (
    _enrich_audit_evidence,
    _enrich_day_over_day,
    _finalize_security_daily_payload,
    _problem_payload,
)
from app.services.security_daily.preview import _next_schedule, _render_preview, _timeline


class SecurityDailyRepository(Protocol):
    async def configuration(self) -> SecurityDailyConfiguration: ...

    async def auto_delivery_configuration(
        self,
    ) -> SecurityDailyAutoDeliveryConfiguration: ...

    async def audit_evidence(
        self, period_start: datetime, period_end: datetime
    ) -> SecurityDailyAuditEvidence | None: ...

    async def ingest_payload(
        self,
        payload: Mapping[str, object],
        *,
        recipient_count: int,
        force: bool = False,
        generation_source: GenerationSource = "auto",
    ) -> bool: ...

    async def mark_unavailable(
        self,
        report_date: date,
        *,
        period_start: datetime,
        period_end: datetime,
        reason: str,
        generation_source: GenerationSource = "auto",
    ) -> bool: ...

    async def update_configuration(
        self,
        update: SecurityDailyConfigurationUpdate,
        *,
        principal: SecurityPrincipal,
        ip: str,
        authorization: AdminAuthorization,
        expected_config_version: int,
    ) -> SecurityDailyConfiguration: ...

    async def overview(self, *, now: datetime) -> SecurityDailyOverview: ...

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage: ...

    async def get_report(self, report_id: int) -> SecurityDailyReportRecord | None: ...

    async def get_latest_report(
        self,
        report_date: date,
        *,
        generation_source: GenerationSource | None = None,
    ) -> SecurityDailyReportRecord | None: ...

    async def exists_sent_delivery(self, report_date: date) -> bool: ...

    async def latest_delivery_request(
        self, report_id: int
    ) -> SecurityDailyDeliveryRequest | None: ...

    async def get_delivery_request(
        self, request_id: UUID
    ) -> SecurityDailyDeliveryRequest | None: ...

    async def request_delivery(
        self,
        report: SecurityDailyReportRecord,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
        control_evidence: str = "missing",
        recipient_set_digest: str = "",
        expected_config_version: int | None = None,
    ) -> SecurityDailyDeliveryRequest: ...

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]: ...

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None: ...

    async def mark_request_failed(self, request_id: UUID, message: str) -> None: ...

    async def mark_request_unknown(self, request_id: UUID, message: str) -> None: ...

    async def mark_delivery_failed(self, report_id: int, message: str) -> bool: ...

    async def mark_configuration_publish_state(
        self,
        *,
        config_version: int,
        publish_state: str,
        operation_id: str | None = None,
        actor: str = "security-daily-config",
    ) -> None: ...


async def generate_security_daily_for_date(
    repository: SecurityDailyRepository,
    control_dir: Path,
    *,
    report_date: date,
    recipient_count: int,
    force: bool = False,
    generation_source: GenerationSource = "auto",
    generated_at: datetime | None = None,
) -> bool:
    """读取指定上海自然日的脱敏快照并写入日报事实表。

    定时任务和管理员手动生成共用此入口，确保两条路径使用完全相同的
    文件大小、JSON 结构和 unavailable 语义；该函数只处理已脱敏结构化证据，
    不会触发邮件投递。记录与 payload 的 generated_at 使用平台生成时刻，
    而不是采集器写快照的时刻，保证每次生成都有独立可区分的生成时间。
    """

    period_start = datetime.combine(report_date, datetime.min.time(), tzinfo=SHANGHAI_TZ)
    period_end = datetime.combine(
        report_date,
        datetime.max.time().replace(microsecond=0),
        tzinfo=SHANGHAI_TZ,
    )
    generation_time = generated_at or datetime.now(SHANGHAI_TZ)
    source = control_dir / "incoming" / f"{report_date.isoformat()}.json"
    try:
        if not source.is_file() or source.stat().st_size > MAX_SECURITY_DAILY_INPUT_BYTES:
            return await repository.mark_unavailable(
                report_date,
                period_start=period_start,
                period_end=period_end,
                reason="安全日报证据源不可用",
                generation_source=generation_source,
            )
        raw = await asyncio.to_thread(source.read_text, encoding="utf-8")
        value: object = json.loads(raw)
        if not isinstance(value, dict):
            raise SecurityDailyValidationError("security report input must be an object")
        # 平台侧派生计算只消费已通过结构、时间和脱敏校验的快照；缺列或
        # 不完整输入必须显式记为 unavailable，不能在 enrich 阶段异常退出。
        payload = validate_security_daily_payload(value)
        evidence = await repository.audit_evidence(period_start, period_end)
        if evidence is not None:
            payload = _enrich_audit_evidence(payload, evidence)
        payload = _finalize_security_daily_payload(payload)
        previous = await repository.get_latest_report(report_date - timedelta(days=1))
        if (
            previous is not None
            and previous.generation_status == "ready"
            and previous.payload is not None
        ):
            payload = _enrich_day_over_day(payload, previous.payload)
        payload["generated_at"] = generation_time.isoformat()
        return await repository.ingest_payload(
            payload,
            recipient_count=recipient_count,
            force=force,
            generation_source=generation_source,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        SecurityDailyValidationError,
    ):
        return await repository.mark_unavailable(
            report_date,
            period_start=period_start,
            period_end=period_end,
            reason="安全日报证据源校验失败",
            generation_source=generation_source,
        )


class SecurityDailyService:
    """编排日报查询、UI 配置和独立 mailer 投递请求。"""

    _PENDING_DELIVERY_RECOVERY_DELAY = timedelta(minutes=5)
    # 与历史补扫的 7 个自然日上限对齐，避免无限期重放同一未发布请求。
    _PENDING_DELIVERY_RECOVERY_DEADLINE = timedelta(days=7)

    def __init__(
        self,
        repository: SecurityDailyRepository,
        control: SecurityDailyControl,
        *,
        control_dir: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
    ) -> None:
        self.repository = repository
        self.control = control
        self.control_dir = control_dir
        self.clock = clock

    async def configuration(self) -> SecurityDailyConfiguration:
        return await self.repository.configuration()

    async def configure(
        self,
        update: SecurityDailyConfigurationUpdate,
        *,
        principal: SecurityPrincipal,
        ip: str,
        authorization: AdminAuthorization,
        expected_config_version: int,
    ) -> SecurityDailyConfiguration:
        """保存管理员配置并同步独立 mailer；Key 不进入日报投递请求。"""

        configuration = await self.repository.update_configuration(
            update,
            principal=principal,
            ip=ip,
            authorization=authorization,
            expected_config_version=expected_config_version,
        )
        await self._publish_configuration(configuration, persist=True)
        return configuration

    async def overview(self) -> SecurityDailyOverview:
        await self._reconcile_configuration(persist=True)
        await self._synchronize_control_results()
        now = self.clock()
        return await self.repository.overview(now=now)

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        ensure_page_offset(query.page, query.page_size)
        await self._reconcile_configuration(persist=True)
        await self._synchronize_control_results()
        return await self.repository.list_reports(query)

    async def get_report(self, report_id: int) -> SecurityDailyReportRecord:
        await self._reconcile_configuration(persist=True)
        await self._synchronize_control_results()
        record = await self.repository.get_report(report_id)
        if record is None:
            raise SecurityDailyNotFound(str(report_id))
        return record

    async def generate_latest(
        self,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyReportRecord:
        """手动无条件重生成上一上海自然日并立即提交投递。

        每次生成都新增一条记录：证据可用时发送脱敏报告；证据不可用时发送
        问题通报并保留 unavailable 记录。投递请求处理中拒绝重生成。
        """

        configuration = await self.repository.configuration()
        if not configuration.enabled:
            raise SecurityDailyUnavailable("安全日报尚未启用")
        if not configuration.api_key or not configuration.recipients:
            raise SecurityDailyUnavailable("安全日报发信配置不完整，无法生成并发送")
        if self.control_dir is None:
            raise SecurityDailyUnavailable("安全日报生成控制目录不可用")

        report_date = self.clock().astimezone(SHANGHAI_TZ).date() - timedelta(days=1)
        existing = await self.repository.get_latest_report(report_date)
        if existing is not None and existing.delivery_status in {"pending", "sending"}:
            raise SecurityDailyStateConflict("该日日报存在处理中的投递请求，请稍后再生成")
        changed = await generate_security_daily_for_date(
            self.repository,
            self.control_dir,
            report_date=report_date,
            recipient_count=len(configuration.recipients),
            force=True,
            generation_source="manual",
            generated_at=self.clock(),
        )
        if not changed:
            raise SecurityDailyUnavailable("证据源不可用，未生成新日报，未发送邮件")
        record = await self.repository.get_latest_report(report_date)
        if record is None:
            raise SecurityDailyUnavailable("安全日报生成未产出记录")
        payload_override: SecurityDailyPayloadData | None = None
        if record.generation_status != "ready" or record.payload is None:
            payload_override = _problem_payload(
                record.report_date,
                period_start=record.period_start,
                period_end=record.period_end,
                generated_at=self.clock(),
                reason=record.last_error or "证据源不可用",
            )
        await self.request_delivery(
            record.id,
            "send",
            principal=principal,
            ip=ip,
            payload_override=payload_override,
        )
        refreshed = await self.repository.get_report(record.id)
        if refreshed is None:
            raise SecurityDailyUnavailable("安全日报生成未产出记录")
        return refreshed

    async def submit_auto_delivery(self, report_date: date) -> SecurityDailyDeliveryRequest | None:
        """自动路径：无论正常报告还是证据不可用，都向收件人提交一次通知。

        每天只提交一次；当天已有任意记录投递成功（含手动）则跳过；发信配置
        不完整时显式记录失败原因，恢复配置后自动补发。
        """

        configuration = await self.repository.auto_delivery_configuration()
        if not configuration.enabled:
            return None
        if await self.repository.exists_sent_delivery(report_date):
            return None
        record = await self.repository.get_latest_report(report_date, generation_source="auto")
        if record is None:
            return None
        if record.delivery_status == "sent":
            return None
        if record.delivery_status in {"pending", "sending", "unknown"}:
            pending_age = self.clock() - record.updated_at
            if pending_age < self._PENDING_DELIVERY_RECOVERY_DELAY:
                return None
        if record.delivery_status == "unknown":
            await self._synchronize_control_results()
            refreshed = await self.repository.get_latest_report(
                report_date, generation_source="auto"
            )
            if refreshed is None or refreshed.delivery_status in {"sent", "unknown"}:
                return None
            record = refreshed
        if record.delivery_status == "failed" and not (record.last_error or "").startswith(
            "安全日报发信配置不完整"
        ):
            return None
        if not configuration.resend_configured or configuration.recipient_count == 0:
            await self.repository.mark_delivery_failed(
                record.id,
                "安全日报发信配置不完整（缺少 Resend Key 或收件人）",
            )
            return None
        if (
            record.generation_status == "ready"
            and record.payload is not None
            and record.generated_at is not None
            and self.control_dir is not None
        ):
            snapshot = self.control_dir / "incoming" / f"{report_date.isoformat()}.json"
            try:
                snapshot_mtime = snapshot.stat().st_mtime
            except OSError:
                snapshot_mtime = None
            if snapshot_mtime is not None and snapshot_mtime > record.generated_at.timestamp():
                # 快照比已入库 payload 更新：直接发送会外发过期数据，
                # 跳过自动投递，等待管理员手动“立即生成”后发送。
                return None
        payload_override: SecurityDailyPayloadData | None = None
        if record.generation_status != "ready" or record.payload is None:
            payload_override = _problem_payload(
                record.report_date,
                period_start=record.period_start,
                period_end=record.period_end,
                generated_at=self.clock(),
                reason=record.last_error or "证据源不可用",
            )
        if not await self._configuration_ready_for_delivery():
            return None
        return await self.request_delivery(
            record.id,
            "send",
            system=True,
            payload_override=payload_override,
        )

    async def preview(self, report_id: int) -> SecurityDailyPreview:
        record = await self.get_report(report_id)
        if record.payload is None or record.generation_status != "ready":
            raise SecurityDailyUnavailable("日报数据不可用，不能生成预览")
        html_preview, text_preview = _render_preview(record.payload)
        return SecurityDailyPreview(
            report_date=record.report_date,
            status=record.status,
            html=html_preview,
            text=text_preview,
            payload=record.payload,
        )

    async def request_delivery(
        self,
        report_id: int,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
        payload_override: SecurityDailyPayloadData | None = None,
    ) -> SecurityDailyDeliveryRequest:
        """发布与 request_id 成对的投递证据，新重试不受旧结果抑制。

        仓储返回新请求后，只在它与已检查的 latest 是同一请求时沿用旧 evidence。
        新 request_id 会重新 inspect，并按 missing/published/claimed/result/unknown
        处理；unknown 禁止盲重试。claimed/result/unknown 的提前返回保留，
        以免破坏原子发布、认领、结果回写和 Resend 幂等。
        """

        automatic = await self.repository.auto_delivery_configuration() if system else None
        enabled = automatic.enabled if automatic is not None else (await self.overview()).enabled
        if not enabled:
            raise SecurityDailyUnavailable("安全日报尚未启用")
        record = (
            await self.repository.get_report(report_id)
            if system
            else await self.get_report(report_id)
        )
        if record is None:
            raise SecurityDailyNotFound(str(report_id))
        submit_payload = payload_override
        if submit_payload is None and (
            record.payload is None or record.generation_status != "ready"
        ):
            raise SecurityDailyUnavailable("日报数据不可用，不能投递")
        if submit_payload is None:
            submit_payload = record.payload
        assert submit_payload is not None
        if action == "retry" and record.delivery_status != "failed":
            raise SecurityDailyStateConflict("只有投递失败的日报允许重试")
        if action == "retry" and (record.last_error or "").startswith("投递结果未知"):
            raise SecurityDailyStateConflict("投递结果未知，禁止盲目重试")
        latest = await self.repository.latest_delivery_request(record.id)
        checked_evidence = (
            await self._inspect_delivery_evidence(latest.request_id)
            if latest is not None
            else "missing"
        )
        if automatic is not None:
            if (
                automatic.publish_state != "file_committed"
                or len(automatic.recipient_set_digest) != 64
            ):
                raise SecurityDailyStateConflict("安全日报配置尚未发布")
            digest = automatic.recipient_set_digest
            version = automatic.config_version
        else:
            configuration = await self.repository.configuration()
            digest = recipient_set_digest(configuration.recipients)
            version = configuration.config_version
        request = await self.repository.request_delivery(
            record,
            action,
            principal=principal,
            ip=ip,
            system=system,
            control_evidence=checked_evidence,
            recipient_set_digest=digest,
            expected_config_version=version,
        )
        if latest is not None and request.request_id == latest.request_id:
            evidence = checked_evidence
        else:
            evidence = await self._inspect_delivery_evidence(request.request_id)
        if evidence == "unknown" and (latest is None or request.request_id != latest.request_id):
            await self.repository.mark_request_unknown(request.request_id, "投递结果未知")
            raise SecurityDailyControlError("投递结果未知")
        if evidence in {"claimed", "result", "unknown"}:
            return request
        resubmit_pending = request.idempotent and request.state == "pending" and action == "send"
        # 控制文件丢失时复用同一 request_id 重建请求，保留 Resend 幂等边界；
        # 已完成请求仍只返回事实，不再次接触投递器。
        if request.idempotent and not resubmit_pending:
            return request
        try:
            if not system:
                await self._publish_configuration(
                    await self.repository.configuration(), persist=True
                )
            await self.control.submit(request, submit_payload)
        except SecurityDailyControlError:
            if await self._submit_still_accepted(request.request_id):
                return request
            raise
        return request

    async def recover_pending_delivery(
        self, request_id: UUID
    ) -> SecurityDailyDeliveryRequest | None:
        """在有界期限内补发同一尚未投递的人工或自动请求。

        只恢复可证明尚未投递的 missing/published，并复用原 request_id。
        claimed、result、unknown 沿用现有协议，不新建投递代次。配置版本或
        收件人摘要不一致时停在围栏外，不在恢复扫描里执行 supersede。
        """

        request = await self.repository.get_delivery_request(request_id)
        if request is None or not request.delivery_id.isdigit():
            return None
        requested_at = request.requested_at
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            return None
        age = self.clock() - requested_at
        if (
            age < self._PENDING_DELIVERY_RECOVERY_DELAY
            or age > self._PENDING_DELIVERY_RECOVERY_DEADLINE
        ):
            return None
        report = await self.repository.get_report(int(request.delivery_id))
        if report is None or report.delivery_status in {"sent", "unknown"}:
            return None
        if request.state in {"sent", "unknown"}:
            return None
        if request.state == "failed" and not (request.error or "").startswith("独立投递器不可用"):
            return None
        if request.state not in {"pending", "failed"}:
            return None
        latest = await self.repository.latest_delivery_request(report.id)
        if latest is None or latest.request_id != request.request_id:
            return None
        evidence = await self._inspect_delivery_evidence(request.request_id)
        if evidence == "unknown":
            if request.state == "pending":
                await self.repository.mark_request_unknown(request.request_id, "投递结果未知")
            return None
        if evidence == "claimed":
            return None
        if evidence == "result":
            result = await self.control.result(request.request_id)
            if result is None:
                return None
            if result.report_date != request.report_date:
                raise SecurityDailyControlError("安全日报投递结果日期不匹配")
            await self.repository.apply_control_result(result)
            return None
        if evidence not in {"missing", "published"}:
            return None
        current = await self.repository.auto_delivery_configuration()
        if not self._delivery_fence_matches(current, request):
            return None
        submit_payload = report.payload
        if report.generation_status != "ready" or submit_payload is None:
            submit_payload = _problem_payload(
                report.report_date,
                period_start=report.period_start,
                period_end=report.period_end,
                generated_at=self.clock(),
                reason=report.last_error or "证据源不可用",
            )
        try:
            await self.control.submit(request, submit_payload)
        except SecurityDailyControlError:
            if await self._submit_still_accepted(request.request_id):
                return request
            raise
        return request

    @staticmethod
    def _delivery_fence_matches(
        current: SecurityDailyAutoDeliveryConfiguration,
        request: SecurityDailyDeliveryRequest,
    ) -> bool:
        """版本和收件人摘要必须同时落在当前已发布配置内。"""

        digest_matches = not (
            len(request.recipient_set_digest) == 64
            and len(current.recipient_set_digest) == 64
            and request.recipient_set_digest != current.recipient_set_digest
        )
        return (
            current.enabled
            and current.resend_configured
            and current.recipient_count >= 1
            and current.publish_state == "file_committed"
            and current.config_version == request.config_version
            and digest_matches
        )

    async def _inspect_delivery_evidence(self, request_id: UUID) -> str:
        """读取单个请求的控制面证据；读失败是 unknown，不能当成 missing。"""

        try:
            return await self.control.inspect_delivery(request_id)
        except SecurityDailyControlError:
            return "unknown"

    async def _publish_configuration(
        self,
        configuration: SecurityDailyConfiguration,
        *,
        persist: bool,
    ) -> None:
        try:
            await self.control.sync_configuration(configuration)
        except SecurityDailyConfigurationSuperseded:
            if persist:
                await self.repository.mark_configuration_publish_state(
                    config_version=configuration.config_version,
                    publish_state="file_pending",
                )
            raise SecurityDailyStateConflict("安全日报配置已被更新版本取代") from None
        if persist:
            await self.repository.mark_configuration_publish_state(
                config_version=configuration.config_version,
                publish_state="file_committed",
            )

    async def _reconcile_configuration(self, *, persist: bool) -> None:
        published = await self.control.published_config_version()
        configuration = await self.repository.configuration()
        if published == configuration.config_version:
            if persist:
                await self.repository.mark_configuration_publish_state(
                    config_version=configuration.config_version,
                    publish_state="file_committed",
                )
            return
        try:
            await self._publish_configuration(configuration, persist=persist)
        except (SecurityDailyControlError, SecurityDailyStateConflict):
            return

    async def _configuration_ready_for_delivery(self) -> bool:
        configuration = await self.repository.auto_delivery_configuration()
        return (
            configuration.config_version > 0
            and configuration.publish_state == "file_committed"
            and len(configuration.recipient_set_digest) == 64
        )

    async def _submit_still_accepted(self, request_id: UUID) -> bool:
        try:
            evidence = await self.control.inspect_delivery(request_id)
        except SecurityDailyControlError:
            await self.repository.mark_request_unknown(request_id, "投递结果未知")
            return False
        if evidence in {"published", "claimed", "result"}:
            return True
        await self.repository.mark_request_failed(request_id, "独立投递器不可用")
        return False

    @staticmethod
    def timeline(record: SecurityDailyReportRecord) -> list[SecurityDailyTimelineEvent]:
        return _timeline(record)

    @staticmethod
    def next_schedule(now: datetime) -> datetime:
        return _next_schedule(now)

    async def _synchronize_control_results(self) -> None:
        """把 mailer 的已完成结果回写事实表；未完成或暂不可读时保持 pending。"""

        for request_id, report_date in await self.repository.pending_delivery_requests():
            result = await self.control.result(request_id)
            if result is None:
                continue
            if result.report_date != report_date:
                raise SecurityDailyControlError("安全日报投递结果日期不匹配")
            await self.repository.apply_control_result(result)
