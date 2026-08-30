"""真实联调页面控制操作的幂等编排与中断恢复。"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.core.auth.accounts import SecurityPrincipal
from app.services.vendor_control_client import ControlAgentUnavailable
from app.services.vendor_test_recipient import RecipientBusy
from vendor_control_protocol import ControlResponse

CONTROL_OPERATION_TYPES = frozenset(
    {
        "install_credentials",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
        "reset_configuration",
    }
)
OPERATION_TYPES = CONTROL_OPERATION_TYPES | {"uat_send"}
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
_RECONCILABLE_STATUSES = frozenset({"requested", "running"})
_REQUESTED_RECONCILE_GRACE = timedelta(seconds=60)
_RESET_RECOVERY_CODES = frozenset(
    {
        "CONTROL_OPERATION_IN_PROGRESS",
        "CONTROL_RESULT_UNKNOWN",
        "CONTROL_STATE_SYNC_FAILED",
    }
)


class VendorTestOperationConflict(RuntimeError):
    """operation ID 已绑定其他不可变合同或状态冲突。"""


class VendorTestOperationPending(RuntimeError):
    """调用结果尚无法安全确认，保留为可对账状态。"""


def uat_biz_id(operation_id: str) -> str:
    """保留旧版 UAT 幂等号映射，供既有记录兼容读取。"""

    try:
        parsed = UUID(operation_id)
    except (ValueError, AttributeError):
        raise VendorTestOperationConflict("operation ID 无效") from None
    if str(parsed) != operation_id:
        raise VendorTestOperationConflict("operation ID 无效")
    return f"uat:{parsed.hex[-28:]}"


def vendor_test_uat_biz_id(operation_id: str) -> str:
    """把 canonical UUID 无碰撞编码为 32 字符合同内的确定性 UAT biz_id。"""

    try:
        parsed = UUID(operation_id)
    except (AttributeError, ValueError):
        raise VendorTestOperationConflict("operation ID 无效") from None
    if str(parsed) != operation_id:
        raise VendorTestOperationConflict("operation ID 无效")
    encoded = urlsafe_b64encode(parsed.bytes).decode("ascii").rstrip("=")
    biz_id = f"vuat:{encoded}"
    if len(biz_id) != 27:
        raise AssertionError("UAT biz_id 编码长度异常")
    return biz_id


@dataclass(frozen=True, slots=True)
class VendorTestOperation:
    operation_id: str
    operation_type: str
    actor: str
    status: str
    safe_code: str | None
    batch_no: str | None
    checkpoint_id: str | None
    requested_at: datetime
    completed_at: datetime | None
    vendor_code: int | None = None
    actor_account_id: int | None = None
    actor_identity_id: int | None = None


@dataclass(frozen=True, slots=True)
class UatBatchResult:
    """发送流水线对页面只暴露的安全 UAT 终态投影。"""

    batch_no: str
    status: str
    safe_code: str | None
    vendor_code: int | None


class VendorTestOperationRepository(Protocol):
    async def reserve_start(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        conflicting_types: frozenset[str],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> VendorTestOperation: ...

    async def claim_control_running(
        self,
        operation_id: str,
    ) -> VendorTestOperation | None: ...

    async def fail_unclaimed_control(
        self,
        operation_id: str,
        *,
        safe_code: str,
    ) -> VendorTestOperation | None: ...

    async def complete(
        self,
        operation_id: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
        batch_no: str | None = None,
    ) -> VendorTestOperation: ...

    async def pending(self) -> tuple[VendorTestOperation, ...]: ...

    async def get(self, operation_id: str) -> VendorTestOperation | None: ...


class VendorControl(Protocol):
    async def request(
        self,
        operation: str,
        *,
        operation_id: str,
        body: dict[str, object],
    ) -> ControlResponse: ...


class VendorTestOperationFinalizer(Protocol):
    async def finalize(self, record: VendorTestOperation) -> None: ...


class VendorTestOperationService:
    """先固化 requested 审计，再调用 agent，最后事务性补记完成审计。"""

    def __init__(
        self,
        repository: VendorTestOperationRepository,
        client: VendorControl,
        *,
        finalizers: Mapping[str, VendorTestOperationFinalizer] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.client = client
        self.finalizers = dict(finalizers or {})
        self._now = now or (lambda: datetime.now(UTC))

    def reset_recovery_due(self, record: VendorTestOperation) -> bool:
        """页面恢复后仅为已超宽限期的同一 reset 安排后台对账。"""

        return (
            record.operation_type == "reset_configuration"
            and record.status in _RECONCILABLE_STATUSES
            and self._now() - record.requested_at >= _REQUESTED_RECONCILE_GRACE
        )

    @staticmethod
    def _is_reset_recovery_pending(
        operation_type: str,
        response: ControlResponse,
    ) -> bool:
        return (
            operation_type == "reset_configuration"
            and response.status == "error"
            and response.safe_code in _RESET_RECOVERY_CODES
            and response.body == {"operation_status": "running"}
        )

    @classmethod
    def _is_execute_pending(
        cls,
        operation_type: str,
        response: ControlResponse,
    ) -> bool:
        if operation_type != "reset_configuration":
            return response.safe_code == "CONTROL_OPERATION_IN_PROGRESS"
        return cls._is_reset_recovery_pending(operation_type, response)

    @staticmethod
    def _is_running_reset_journal(
        record: VendorTestOperation,
        response: ControlResponse,
    ) -> bool:
        return (
            record.operation_type == "reset_configuration"
            and response.status == "error"
            and response.safe_code == "CONTROL_OPERATION_IN_PROGRESS"
            and response.body == {"operation_status": "running"}
        )

    async def start(
        self,
        *,
        operation_id: str,
        operation_type: str,
        principal: SecurityPrincipal,
        body: dict[str, object],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> VendorTestOperation:
        """固化 requested 后立即返回；body 只用于校验调用合同，不持久化。"""

        del body
        if operation_type not in CONTROL_OPERATION_TYPES:
            raise VendorTestOperationConflict("控制操作类型无效")
        conflicting_types = (
            OPERATION_TYPES
            if operation_type == "reset_configuration"
            else CONTROL_OPERATION_TYPES
        )
        return await self.repository.reserve_start(
            operation_id,
            operation_type,
            principal=principal,
            conflicting_types=frozenset(conflicting_types),
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
        )

    async def execute_background(
        self,
        *,
        operation_id: str,
        operation_type: str,
        principal: SecurityPrincipal,
        body: dict[str, object],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> None:
        """响应提交后执行固定 agent 操作；中断时保留 running 等待对账。"""

        try:
            await self.execute_reserved(
                operation_id=operation_id,
                operation_type=operation_type,
                principal=principal,
                body=body,
                batch_no=batch_no,
                checkpoint_id=checkpoint_id,
            )
        except (VendorTestOperationConflict, VendorTestOperationPending):
            return

    async def execute(
        self,
        *,
        operation_id: str,
        operation_type: str,
        principal: SecurityPrincipal,
        body: dict[str, object],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> VendorTestOperation:
        await self.start(
            operation_id=operation_id,
            operation_type=operation_type,
            principal=principal,
            body={},
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
        )
        return await self.execute_reserved(
            operation_id=operation_id,
            operation_type=operation_type,
            principal=principal,
            body=body,
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
        )

    async def execute_reserved(
        self,
        *,
        operation_id: str,
        operation_type: str,
        principal: SecurityPrincipal,
        body: dict[str, object],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> VendorTestOperation:
        """仅执行已原子预留的控制操作，永不隐式创建 operation。"""

        if operation_type not in CONTROL_OPERATION_TYPES:
            raise VendorTestOperationConflict("控制操作类型无效")
        record = await self.repository.get(operation_id)
        if (
            record is None
            or record.operation_type != operation_type
            or record.actor_account_id != principal.account_id
        ):
            raise VendorTestOperationConflict("控制操作未安全预留")
        if record.status in TERMINAL_STATUSES:
            return record
        if record.status == "requested":
            claimed = await self.repository.claim_control_running(operation_id)
            if claimed is None:
                current = await self.repository.get(operation_id)
                if (
                    current is None
                    or current.operation_type != operation_type
                    or current.actor_account_id != principal.account_id
                ):
                    raise VendorTestOperationConflict("控制操作未安全预留")
                if current.status in TERMINAL_STATUSES:
                    return current
                if current.status == "running":
                    raise VendorTestOperationPending("控制操作等待安全对账")
                raise VendorTestOperationConflict("控制操作状态冲突")
            record = claimed
        elif record.status == "running":
            raise VendorTestOperationPending("控制操作等待安全对账")
        if record.status != "running":
            raise VendorTestOperationConflict("控制操作状态冲突")
        try:
            agent_body = (
                {**body, "actor": principal.login_name}
                if operation_type in {"install_credentials", "rotate_credentials"}
                else body
            )
            response = await self.client.request(
                operation_type,
                operation_id=operation_id,
                body=agent_body,
            )
        except ControlAgentUnavailable:
            raise VendorTestOperationPending("控制操作等待安全对账") from None
        if self._is_execute_pending(operation_type, response):
            raise VendorTestOperationPending("控制操作等待安全对账")
        response_checkpoint = response.body.get("checkpoint_id")
        terminal_checkpoint = (
            response_checkpoint if isinstance(response_checkpoint, str) else checkpoint_id
        )
        status = "succeeded" if response.status == "ok" else "failed"
        safe_code = None if status == "succeeded" else response.safe_code
        vendor_code_value = response.body.get("vendor_code")
        vendor_code = (
            vendor_code_value
            if type(vendor_code_value) is int and 1 <= vendor_code_value <= 99_999
            else None
        )
        return await self._complete_terminal(
            record,
            status=status,
            safe_code=safe_code
            or ("CONTROL_OPERATION_FAILED" if status == "failed" else None),
            checkpoint_id=terminal_checkpoint,
            vendor_code=vendor_code,
        )

    async def reconcile_once(self) -> int:
        """按原 operation ID 查询 agent journal；未知结果继续保持 fail-closed。"""

        reconciled = 0
        for record in await self.repository.pending():
            if (
                record.status not in _RECONCILABLE_STATUSES
                or record.operation_type not in CONTROL_OPERATION_TYPES
            ):
                continue
            if (
                record.status == "requested"
                and self._now() - record.requested_at < _REQUESTED_RECONCILE_GRACE
            ):
                continue
            try:
                response = await self.client.request(
                    "status",
                    operation_id=record.operation_id,
                    body={},
                )
            except ControlAgentUnavailable:
                continue
            if self._is_running_reset_journal(record, response):
                try:
                    response = await self.client.request(
                        "reset_configuration",
                        operation_id=record.operation_id,
                        body={},
                    )
                except ControlAgentUnavailable:
                    continue
                if self._is_reset_recovery_pending(record.operation_type, response):
                    continue
            if response.safe_code == "OPERATION_NOT_FOUND":
                if record.status != "requested":
                    continue
                failed = await self.repository.fail_unclaimed_control(
                    record.operation_id,
                    safe_code="CONTROL_OPERATION_NOT_FOUND",
                )
                if failed is not None:
                    reconciled += 1
                continue
            journal_status = response.body.get("operation_status")
            if journal_status not in TERMINAL_STATUSES:
                continue
            safe_code = None if journal_status == "succeeded" else response.safe_code
            checkpoint_value = response.body.get("checkpoint_id")
            checkpoint_id = (
                checkpoint_value if type(checkpoint_value) is str else None
            )
            vendor_code_value = response.body.get("vendor_code")
            vendor_code = (
                vendor_code_value
                if type(vendor_code_value) is int
                and safe_code == "VENDOR_ERROR"
                and 1 <= vendor_code_value <= 99_999
                else None
            )
            try:
                await self._complete_terminal(
                    record,
                    status=str(journal_status),
                    safe_code=safe_code
                    or (
                        "CONTROL_OPERATION_FAILED"
                        if journal_status == "failed"
                        else None
                    ),
                    checkpoint_id=checkpoint_id,
                    vendor_code=vendor_code,
                )
            except VendorTestOperationPending:
                continue
            reconciled += 1
        return reconciled

    async def _complete_terminal(
        self,
        record: VendorTestOperation,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None,
        vendor_code: int | None,
    ) -> VendorTestOperation:
        """成功终态须先完成幂等业务收尾，再固化 operation 完成审计。"""

        if status == "succeeded":
            finalizer = self.finalizers.get(record.operation_type)
            if finalizer is not None:
                try:
                    await finalizer.finalize(record)
                except RecipientBusy:
                    raise VendorTestOperationPending(
                        "控制操作等待安全对账"
                    ) from None
        return await self.repository.complete(
            record.operation_id,
            status=status,
            safe_code=safe_code,
            checkpoint_id=checkpoint_id,
            vendor_code=vendor_code,
            batch_no=None,
        )

    async def get(self, operation_id: str) -> VendorTestOperation | None:
        return await self.repository.get(operation_id)
