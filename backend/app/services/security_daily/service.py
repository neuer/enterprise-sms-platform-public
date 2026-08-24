"""安全日报生成与投递的编排服务。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from app.core.auth.accounts import SecurityPrincipal
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
    SecurityDailyPreview,
    SecurityDailyQuery,
    SecurityDailyReportRecord,
    SecurityDailyStateConflict,
    SecurityDailyUnavailable,
    SecurityDailyValidationError,
    validate_security_daily_payload,
)
from app.services.security_daily.control import (
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
        payload: dict[str, Any],
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

    async def request_delivery(
        self,
        report: SecurityDailyReportRecord,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
    ) -> SecurityDailyDeliveryRequest: ...

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]: ...

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None: ...

    async def mark_request_failed(self, request_id: UUID, message: str) -> None: ...

    async def mark_delivery_failed(self, report_id: int, message: str) -> bool: ...


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
        value: Any = json.loads(raw)
        if not isinstance(value, dict):
            raise SecurityDailyValidationError("security report input must be an object")
        # 平台侧派生计算只消费已通过结构、时间和脱敏校验的快照；缺列或
        # 不完整输入必须显式记为 unavailable，不能在 enrich 阶段异常退出。
        value = validate_security_daily_payload(value)
        evidence = await repository.audit_evidence(period_start, period_end)
        if evidence is not None:
            value = _enrich_audit_evidence(value, evidence)
        value = _finalize_security_daily_payload(value)
        previous = await repository.get_latest_report(report_date - timedelta(days=1))
        if (
            previous is not None
            and previous.generation_status == "ready"
            and previous.payload is not None
        ):
            value = _enrich_day_over_day(value, previous.payload)
        value["generated_at"] = generation_time.isoformat()
        return await repository.ingest_payload(
            value,
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
    ) -> SecurityDailyConfiguration:
        """保存管理员配置并同步独立 mailer；Key 不进入日报投递请求。"""

        configuration = await self.repository.update_configuration(
            update,
            principal=principal,
            ip=ip,
        )
        await self.control.sync_configuration(configuration)
        return configuration

    async def overview(self) -> SecurityDailyOverview:
        await self._synchronize_control_results()
        now = self.clock()
        return await self.repository.overview(now=now)

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        await self._synchronize_control_results()
        return await self.repository.list_reports(query)

    async def get_report(self, report_id: int) -> SecurityDailyReportRecord:
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
        payload_override: dict[str, Any] | None = None
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
        record = await self.repository.get_latest_report(
            report_date, generation_source="auto"
        )
        if record is None:
            return None
        if record.delivery_status == "sent":
            return None
        if record.delivery_status in {"pending", "sending"}:
            pending_age = self.clock() - record.updated_at
            if pending_age < self._PENDING_DELIVERY_RECOVERY_DELAY:
                return None
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
        payload_override: dict[str, Any] | None = None
        if record.generation_status != "ready" or record.payload is None:
            payload_override = _problem_payload(
                record.report_date,
                period_start=record.period_start,
                period_end=record.period_end,
                generated_at=self.clock(),
                reason=record.last_error or "证据源不可用",
            )
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
        payload_override: dict[str, Any] | None = None,
    ) -> SecurityDailyDeliveryRequest:
        overview = await self.overview()
        if not overview.enabled:
            raise SecurityDailyUnavailable("安全日报尚未启用")
        record = await self.get_report(report_id)
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
        request = await self.repository.request_delivery(
            record,
            action,
            principal=principal,
            ip=ip,
            system=system,
        )
        resubmit_pending = (
            request.idempotent and request.state == "pending" and action == "send"
        )
        # 控制文件丢失时复用同一 request_id 重建请求，保留 Resend 幂等边界；
        # 已完成请求仍只返回事实，不再次接触投递器。
        if request.idempotent and not resubmit_pending:
            return request
        try:
            if not system:
                await self.control.sync_configuration(await self.repository.configuration())
            await self.control.submit(request, submit_payload)
        except SecurityDailyControlError:
            await self.repository.mark_request_failed(request.request_id, "独立投递器不可用")
            raise
        return request

    @staticmethod
    def timeline(record: SecurityDailyReportRecord) -> list[dict[str, Any]]:
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
