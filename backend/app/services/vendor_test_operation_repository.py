"""真实联调控制 operation 的 PostgreSQL 事实源与安全审计。"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.principal_context import current_audit_principal
from app.core.runtime_resources import (
    bind_connection_audit_subject,
    bind_connection_system_audit,
    database_engine,
)
from app.services.vendor_test_lifecycle import lock_vendor_test_lifecycle
from app.services.vendor_test_operation import (
    CONTROL_OPERATION_TYPES,
    OPERATION_TYPES,
    TERMINAL_STATUSES,
    UatBatchResult,
    VendorTestOperation,
    VendorTestOperationConflict,
    vendor_test_uat_biz_id,
)
from app.settings import Settings, get_settings

_COLUMNS = (
    "id,operation_type,actor,actor_account_id,actor_identity_id,"
    "status,safe_code,vendor_code,batch_no,checkpoint_id,"
    "requested_at,completed_at"
)
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SAFE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
UAT_ACCEPTANCE_LEASE_SECONDS = 60
_UAT_ACCEPTANCE_LOCK_PREFIX = "vendor-uat-accept:"
_CONTROL_OPERATION_SQL = ",".join(
    f"'{operation_type}'" for operation_type in sorted(CONTROL_OPERATION_TYPES)
)


def _one_or_none(result: Any) -> Any:
    return result.mappings().one_or_none()


def _record(row: Any) -> VendorTestOperation:
    return VendorTestOperation(
        operation_id=str(row["id"]),
        operation_type=str(row["operation_type"]),
        actor=str(row["actor"]),
        status=str(row["status"]),
        safe_code=str(row["safe_code"]) if row["safe_code"] is not None else None,
        batch_no=str(row["batch_no"]).strip() if row["batch_no"] is not None else None,
        checkpoint_id=(str(row["checkpoint_id"]) if row["checkpoint_id"] is not None else None),
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        vendor_code=(int(row["vendor_code"]) if row["vendor_code"] is not None else None),
        actor_account_id=(
            int(row["actor_account_id"]) if row["actor_account_id"] is not None else None
        ),
        actor_identity_id=(
            int(row["actor_identity_id"]) if row["actor_identity_id"] is not None else None
        ),
    )


def _operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise VendorTestOperationConflict("operation ID 无效") from None
    if str(parsed) != value:
        raise VendorTestOperationConflict("operation ID 无效")
    return value


def _reference(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    if len(value) > maximum or _SAFE_REFERENCE.fullmatch(value) is None:
        raise VendorTestOperationConflict("operation 引用无效")
    return value


class SqlVendorTestOperationRepository:
    """只持久化状态和安全引用，operation 请求体没有仓储入口。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    @staticmethod
    def _validate_contract(
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        batch_no: str | None,
        checkpoint_id: str | None,
    ) -> tuple[str, str | None, str | None]:
        operation_id = _operation_id(operation_id)
        if operation_type not in OPERATION_TYPES:
            raise VendorTestOperationConflict("operation 合同无效")
        return (
            operation_id,
            _reference(batch_no, maximum=64),
            _reference(checkpoint_id, maximum=128),
        )

    @staticmethod
    def _assert_contract(
        record: VendorTestOperation,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        batch_no: str | None,
        checkpoint_id: str | None,
    ) -> None:
        if (
            record.operation_type != operation_type
            or record.actor_account_id != principal.account_id
            or record.batch_no != batch_no
            or record.checkpoint_id != checkpoint_id
        ):
            raise VendorTestOperationConflict("operation ID 已被占用")

    async def reserve_start(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        conflicting_types: frozenset[str],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> VendorTestOperation:
        """在共享生命周期锁内原子完成冲突检查与 requested 预留。"""

        operation_id, batch_no, checkpoint_id = self._validate_contract(
            operation_id,
            operation_type,
            principal=principal,
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
        )
        if not conflicting_types or not conflicting_types.issubset(OPERATION_TYPES):
            raise VendorTestOperationConflict("operation 冲突集合无效")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_vendor_test_lifecycle(connection)
                existing = await connection.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM vendor_test_operation "
                        "WHERE id=CAST(:id AS uuid) FOR UPDATE"
                    ),
                    {"id": operation_id},
                )
                row = _one_or_none(existing)
                if row is not None:
                    record = _record(row)
                    self._assert_contract(
                        record,
                        operation_type,
                        principal=principal,
                        batch_no=batch_no,
                        checkpoint_id=checkpoint_id,
                    )
                    return record
                placeholders: list[str] = []
                conflict_params: dict[str, object] = {"id": operation_id}
                for index, item in enumerate(sorted(conflicting_types)):
                    key = f"conflict_type_{index}"
                    placeholders.append(f":{key}")
                    conflict_params[key] = item
                conflict = await connection.execute(
                    text(
                        "SELECT id FROM vendor_test_operation "
                        "WHERE id<>CAST(:id AS uuid) "
                        "AND status IN ('requested','running') "
                        f"AND operation_type IN ({','.join(placeholders)}) "
                        "LIMIT 1"
                    ),
                    conflict_params,
                )
                if _one_or_none(conflict) is not None:
                    raise VendorTestOperationConflict("已有联调操作正在执行")
                return await self._request_in_transaction(
                    connection,
                    operation_id,
                    operation_type,
                    principal=principal,
                    batch_no=batch_no,
                    checkpoint_id=checkpoint_id,
                )
        finally:
            await engine.dispose()

    async def _request_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        batch_no: str | None,
        checkpoint_id: str | None,
    ) -> VendorTestOperation:
        inserted = await connection.execute(
            text(
                f"""
                INSERT INTO vendor_test_operation(
                  id,operation_type,actor,actor_account_id,actor_identity_id,
                  status,batch_no,checkpoint_id,
                  lease_expires_at
                ) VALUES(
                  CAST(:id AS uuid),:operation_type,:actor,
                  :actor_account_id,:actor_identity_id,'requested',
                  CAST(:batch_no AS varchar(64)),
                  CAST(:checkpoint_id AS varchar(128)),
                  CASE
                    WHEN CAST(:operation_type AS varchar(32))='uat_send'
                      AND CAST(:batch_no AS varchar(64)) IS NULL
                    THEN now()+make_interval(secs=>:lease_seconds)
                    ELSE NULL
                  END
                ) ON CONFLICT(id) DO NOTHING
                RETURNING {_COLUMNS}
                """
            ),
            {
                "id": operation_id,
                "operation_type": operation_type,
                "actor": principal.login_name,
                "actor_account_id": principal.account_id,
                "actor_identity_id": principal.identity_id,
                "batch_no": batch_no,
                "checkpoint_id": checkpoint_id,
                "lease_seconds": UAT_ACCEPTANCE_LEASE_SECONDS,
            },
        )
        row = _one_or_none(inserted)
        if row is not None:
            record = _record(row)
            await self._audit(
                connection,
                action="vendor_test_operation_requested",
                record=record,
            )
            return record
        existing = await connection.execute(
            text(
                f"SELECT {_COLUMNS} FROM vendor_test_operation "
                "WHERE id=CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": operation_id},
        )
        row = _one_or_none(existing)
        if row is None:
            raise VendorTestOperationConflict("operation 不存在")
        record = _record(row)
        self._assert_contract(
            record,
            operation_type,
            principal=principal,
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
        )
        return record

    async def mark_running(self, operation_id: str) -> VendorTestOperation:
        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation SET
                          status='running',
                          lease_expires_at=CASE
                            WHEN operation_type='uat_send' AND batch_no IS NULL
                            THEN now()+make_interval(secs=>:lease_seconds)
                            ELSE lease_expires_at
                          END
                        WHERE id=CAST(:id AS uuid) AND status='requested'
                          AND (
                            operation_type<>'uat_send'
                            OR lease_expires_at > now()
                          )
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {
                        "id": operation_id,
                        "lease_seconds": UAT_ACCEPTANCE_LEASE_SECONDS,
                    },
                )
                row = _one_or_none(updated)
                if row is not None:
                    return _record(row)
                current = await connection.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM vendor_test_operation WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": operation_id},
                )
                row = _one_or_none(current)
                if row is None:
                    raise VendorTestOperationConflict("operation 不存在")
                record = _record(row)
                if record.status not in {"running", *TERMINAL_STATUSES}:
                    raise VendorTestOperationConflict("operation 状态冲突")
                return record
        finally:
            await engine.dispose()

    async def claim_control_running(
        self,
        operation_id: str,
    ) -> VendorTestOperation | None:
        """只有原子完成 control requested→running 的调用方可触达 agent。"""

        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation SET status='running'
                        WHERE id=CAST(:id AS uuid)
                          AND status='requested'
                          AND operation_type IN ({_CONTROL_OPERATION_SQL})
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {"id": operation_id},
                )
                row = _one_or_none(updated)
                return _record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def fail_unclaimed_control(
        self,
        operation_id: str,
        *,
        safe_code: str,
    ) -> VendorTestOperation | None:
        """仅当 control 尚未被执行者领取时，原子收敛缺失 journal。"""

        operation_id = _operation_id(operation_id)
        if _SAFE_CODE.fullmatch(safe_code) is None:
            raise VendorTestOperationConflict("operation 安全码无效")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation SET
                          status='failed',safe_code=:safe_code,
                          vendor_code=NULL,lease_expires_at=NULL,
                          completed_at=now()
                        WHERE id=CAST(:id AS uuid)
                          AND status='requested'
                          AND operation_type IN ({_CONTROL_OPERATION_SQL})
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {"id": operation_id, "safe_code": safe_code},
                )
                row = _one_or_none(updated)
                if row is None:
                    return None
                record = _record(row)
                await self._audit(
                    connection,
                    action="vendor_test_operation_completed",
                    record=record,
                )
                return record
        finally:
            await engine.dispose()

    async def attach_batch(
        self,
        operation_id: str,
        *,
        batch_no: str,
    ) -> VendorTestOperation:
        """把 deterministic UAT batch 安全引用附着到 running operation。"""

        operation_id = _operation_id(operation_id)
        validated_batch_no = _reference(batch_no, maximum=64)
        if validated_batch_no is None:
            raise VendorTestOperationConflict("operation batch 引用无效")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation SET
                          batch_no=:batch_no,lease_expires_at=NULL
                        WHERE id=CAST(:id AS uuid)
                          AND operation_type='uat_send'
                          AND status='running'
                          AND (batch_no IS NULL OR batch_no=:batch_no)
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {"id": operation_id, "batch_no": validated_batch_no},
                )
                row = _one_or_none(updated)
                if row is None:
                    raise VendorTestOperationConflict("operation batch 状态冲突")
                record = _record(row)
                await self._audit(
                    connection,
                    action="vendor_test_operation_batch_attached",
                    record=record,
                )
                return record
        finally:
            await engine.dispose()

    async def claim_uat_running(
        self,
        operation_id: str,
    ) -> VendorTestOperation | None:
        """只有完成 requested→running 的调用方才取得唯一 accept 权限。"""

        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation SET
                          status='running',
                          lease_expires_at=now()+make_interval(secs=>:lease_seconds)
                        WHERE id=CAST(:id AS uuid)
                          AND operation_type='uat_send'
                          AND status='requested'
                          AND batch_no IS NULL
                          AND lease_expires_at > now()
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {
                        "id": operation_id,
                        "lease_seconds": UAT_ACCEPTANCE_LEASE_SECONDS,
                    },
                )
                row = _one_or_none(updated)
                return _record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def heartbeat(self, operation_id: str) -> bool:
        """仅续租仍归当前请求拥有且尚未过期的 pre-batch UAT。"""

        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        """
                        UPDATE vendor_test_operation SET
                          lease_expires_at=now()+make_interval(secs=>:lease_seconds)
                        WHERE id=CAST(:id AS uuid)
                          AND operation_type='uat_send'
                          AND status IN ('requested','running')
                          AND batch_no IS NULL
                          AND lease_expires_at > now()
                        RETURNING id
                        """
                    ),
                    {
                        "id": operation_id,
                        "lease_seconds": UAT_ACCEPTANCE_LEASE_SECONDS,
                    },
                )
                return _one_or_none(updated) is not None
        finally:
            await engine.dispose()

    async def prepare_uat_acceptance(self, operation_id: str) -> bool:
        """在 guard 内确认 running lease 仍有效，并刷新至完整窗口。"""

        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        """
                        UPDATE vendor_test_operation SET
                          lease_expires_at=now()+make_interval(secs=>:lease_seconds)
                        WHERE id=CAST(:id AS uuid)
                          AND operation_type='uat_send'
                          AND status='running'
                          AND batch_no IS NULL
                          AND lease_expires_at > now()
                        RETURNING id
                        """
                    ),
                    {
                        "id": operation_id,
                        "lease_seconds": UAT_ACCEPTANCE_LEASE_SECONDS,
                    },
                )
                return _one_or_none(updated) is not None
        finally:
            await engine.dispose()

    @asynccontextmanager
    async def acceptance_guard(
        self,
        operation_id: str,
    ) -> AsyncIterator[None]:
        """串行化 accept/attach 与过期收尾，避免迟到批次越过终态。"""

        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        SELECT pg_advisory_xact_lock(
                          hashtextextended(:lock_name,0)
                        )
                        """
                    ),
                    {"lock_name": f"{_UAT_ACCEPTANCE_LOCK_PREFIX}{operation_id}"},
                )
                yield
        finally:
            await engine.dispose()

    async def expire_uat_if_stale(
        self,
        operation_id: str,
        *,
        safe_code: str,
    ) -> VendorTestOperation | None:
        """在 operation guard 内以 PostgreSQL batch 事实源原子关闭失联 UAT。"""

        operation_id = _operation_id(operation_id)
        if _SAFE_CODE.fullmatch(safe_code) is None:
            raise VendorTestOperationConflict("operation 安全码无效")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                acquired = await connection.execute(
                    text(
                        """
                        SELECT pg_try_advisory_xact_lock(
                          hashtextextended(:lock_name,0)
                        )
                        """
                    ),
                    {"lock_name": f"{_UAT_ACCEPTANCE_LOCK_PREFIX}{operation_id}"},
                )
                if not bool(acquired.scalar_one()):
                    return None
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation AS operation SET
                          status='failed',safe_code=:safe_code,
                          vendor_code=NULL,lease_expires_at=NULL,
                          completed_at=now()
                        WHERE operation.id=CAST(:id AS uuid)
                          AND operation.operation_type='uat_send'
                          AND operation.status IN ('requested','running')
                          AND operation.batch_no IS NULL
                          AND operation.lease_expires_at <= now()
                          AND NOT EXISTS (
                            SELECT 1 FROM sms_batch batch
                            WHERE batch.biz_id=:biz_id
                              AND batch.channel='web'
                              AND batch.is_test=true
                              AND batch.app_id IS NOT NULL
                          )
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {
                        "id": operation_id,
                        "safe_code": safe_code,
                        "biz_id": vendor_test_uat_biz_id(operation_id),
                    },
                )
                row = _one_or_none(updated)
                if row is None:
                    return None
                record = _record(row)
                await self._audit(
                    connection,
                    action="vendor_test_operation_completed",
                    record=record,
                )
                return record
        finally:
            await engine.dispose()

    async def complete(
        self,
        operation_id: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
        batch_no: str | None = None,
    ) -> VendorTestOperation:
        operation_id = _operation_id(operation_id)
        if status not in TERMINAL_STATUSES:
            raise VendorTestOperationConflict("operation 终态无效")
        if status == "succeeded" and safe_code is not None:
            raise VendorTestOperationConflict("成功 operation 不得有错误码")
        if status == "failed" and (safe_code is None or _SAFE_CODE.fullmatch(safe_code) is None):
            raise VendorTestOperationConflict("operation 安全码无效")
        if vendor_code is not None and (
            type(vendor_code) is not int
            or not 1 <= vendor_code <= 99_999
            or safe_code != "VENDOR_ERROR"
        ):
            raise VendorTestOperationConflict("厂商错误码无效")
        checkpoint_id = _reference(checkpoint_id, maximum=128)
        batch_no = _reference(batch_no, maximum=64)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_operation SET
                          status=:status,safe_code=:safe_code,
                          vendor_code=:vendor_code,
                          batch_no=COALESCE(:batch_no,batch_no),
                          checkpoint_id=COALESCE(:checkpoint_id,checkpoint_id),
                          lease_expires_at=NULL,
                          completed_at=now()
                        WHERE id=CAST(:id AS uuid)
                          AND status IN ('requested','running')
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {
                        "id": operation_id,
                        "status": status,
                        "safe_code": safe_code,
                        "vendor_code": vendor_code,
                        "batch_no": batch_no,
                        "checkpoint_id": checkpoint_id,
                    },
                )
                row = _one_or_none(updated)
                if row is not None:
                    record = _record(row)
                    await self._audit(
                        connection,
                        action="vendor_test_operation_completed",
                        record=record,
                    )
                    return record
                current = await connection.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM vendor_test_operation "
                        "WHERE id=CAST(:id AS uuid) FOR UPDATE"
                    ),
                    {"id": operation_id},
                )
                row = _one_or_none(current)
                if row is None:
                    raise VendorTestOperationConflict("operation 不存在")
                record = _record(row)
                expected_checkpoint = checkpoint_id or record.checkpoint_id
                expected_batch = batch_no or record.batch_no
                if (
                    record.status != status
                    or record.safe_code != safe_code
                    or record.vendor_code != vendor_code
                    or record.batch_no != expected_batch
                    or record.checkpoint_id != expected_checkpoint
                ):
                    raise VendorTestOperationConflict("operation 终态冲突")
                return record
        finally:
            await engine.dispose()

    async def pending(self) -> tuple[VendorTestOperation, ...]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        SELECT {_COLUMNS} FROM vendor_test_operation
                        WHERE status IN ('requested','running')
                        ORDER BY requested_at,id LIMIT 100
                        """
                    )
                )
                return tuple(_record(row) for row in result.mappings())
        finally:
            await engine.dispose()

    async def get(self, operation_id: str) -> VendorTestOperation | None:
        operation_id = _operation_id(operation_id)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM vendor_test_operation WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": operation_id},
                )
                row = _one_or_none(result)
                return _record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def uat_result(
        self,
        operation_id: str,
        *,
        batch_no: str | None,
    ) -> UatBatchResult | None:
        """按安全引用观察 Send 终态，不读取手机号、内容或厂商消息。"""

        operation_id = _operation_id(operation_id)
        batch_no = _reference(batch_no, maximum=64)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT trim(b.batch_no) batch_no,b.status batch_status,
                          c.status chunk_status,c.vendor_code
                        FROM sms_batch b
                        LEFT JOIN sms_chunk c ON c.batch_id=b.id
                        WHERE b.channel='web'
                          AND b.is_test=true
                          AND b.app_id IS NOT NULL
                          AND (
                            (
                              CAST(:batch_no AS varchar(64)) IS NOT NULL
                              AND trim(b.batch_no)=CAST(:batch_no AS varchar(64))
                            )
                            OR b.biz_id=:biz_id
                          )
                        ORDER BY b.id,c.id
                        """
                    ),
                    {
                        "batch_no": batch_no,
                        "biz_id": vendor_test_uat_biz_id(operation_id),
                    },
                )
                rows = list(result.mappings())
        finally:
            await engine.dispose()
        if not rows:
            return None
        batch_numbers = {str(row["batch_no"]).strip() for row in rows}
        if len(batch_numbers) != 1:
            raise VendorTestOperationConflict("UAT batch 引用冲突")
        resolved_batch = next(iter(batch_numbers))
        statuses = [str(row["chunk_status"]) for row in rows if row["chunk_status"] is not None]
        if any(
            status in {"pending", "submitting", "retrying", "split_capacity_blocked"}
            for status in statuses
        ):
            return UatBatchResult(resolved_batch, "running", None, None)
        if "uncertain" in statuses:
            return UatBatchResult(
                resolved_batch,
                "failed",
                "RESULT_UNCERTAIN",
                None,
            )
        if "failed" in statuses:
            codes = [
                int(row["vendor_code"])
                for row in rows
                if row["chunk_status"] == "failed"
                and type(row["vendor_code"]) is int
                and 1 <= int(row["vendor_code"]) <= 99_999
            ]
            return UatBatchResult(
                resolved_batch,
                "failed",
                "VENDOR_ERROR" if codes else "UAT_SEND_REJECTED",
                codes[0] if codes else None,
            )
        if statuses and all(status == "submitted" for status in statuses):
            return UatBatchResult(resolved_batch, "succeeded", None, None)
        batch_statuses = {str(row["batch_status"]) for row in rows}
        if batch_statuses & {"rejected", "expired", "cancelled"}:
            return UatBatchResult(
                resolved_batch,
                "failed",
                "UAT_SEND_REJECTED",
                None,
            )
        return UatBatchResult(resolved_batch, "running", None, None)

    @staticmethod
    async def _audit(
        connection: Any,
        *,
        action: str,
        record: VendorTestOperation,
    ) -> None:
        payload: dict[str, object] = {
            "count": 1,
            "operation_id": record.operation_id,
        }
        if record.batch_no is not None:
            payload["batch_no"] = record.batch_no
        if record.checkpoint_id is not None:
            payload["checkpoint_id"] = record.checkpoint_id
        if record.vendor_code is not None:
            payload["vendor_code"] = record.vendor_code
        principal = current_audit_principal()
        stable_actor = record.actor_account_id is not None and principal is not None
        if stable_actor:
            if record.actor_identity_id is None:
                raise ValueError("vendor test audit identity is incomplete")
            await bind_connection_audit_subject(
                connection,
                subject_kind="human",
                actor_name=record.actor,
                account_id=record.actor_account_id,
                identity_id=record.actor_identity_id,
            )
        else:
            await bind_connection_system_audit(
                connection,
                actor_name="vendor-test-reconciler",
                action=action,
            )
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(
                  actor,actor_subject_kind,actor_account_id,actor_identity_id,
                  action,object_type,object_id,after_val
                )
                VALUES(
                  :actor,:actor_subject_kind,:actor_account_id,:actor_identity_id,
                  :action,'vendor_test_operation',:object_id,CAST(:after AS jsonb)
                )
                """
            ),
            {
                "actor": (
                    record.actor if stable_actor else "vendor-test-reconciler"
                ),
                "actor_subject_kind": (
                    "human" if stable_actor else "system"
                ),
                "actor_account_id": record.actor_account_id if stable_actor else None,
                "actor_identity_id": record.actor_identity_id if stable_actor else None,
                "action": action,
                "object_id": record.operation_id,
                "after": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            },
        )
