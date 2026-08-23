"""异步导出 worker：逐行受控解密并直接写认证密文帧。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.services.content_protection import decrypt_batch_display_content
from app.services.crypto import CryptoService
from app.services.export import MAX_EXPORT_ROWS, ExportTooLarge
from app.services.export_file import ExportFileCodec
from app.services.export_repository import ExportClaim, ExportLeaseLost

CSV_HEADER = (
    "created_at",
    "batch_no",
    "category",
    "phone",
    "status",
    "report_desc",
    "report_time",
    "content",
)
UNMATCHED_CSV_HEADER = (
    "created_at",
    "custom_id",
    "vendor_task_id",
    "phone",
    "report_status",
    "report_desc",
    "report_time",
)


class ExportWorkerRepository(Protocol):
    async def claim(
        self,
        task_id: int,
        *,
        lease_seconds: int,
    ) -> ExportClaim | None: ...

    async def heartbeat(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool: ...

    def rows(self, claim: ExportClaim) -> AsyncIterator[dict[str, object]]: ...

    async def mark_done(
        self,
        task_id: int,
        *,
        lease_id: UUID,
        file_path: str,
        row_count: int,
    ) -> None: ...

    async def mark_failed(self, task_id: int, *, lease_id: UUID) -> None: ...


def _text(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


class ExportWorker:
    """领取数据库租约；明文手机号仅存在于单行 CSV 构造调用栈。"""

    def __init__(
        self,
        repository: ExportWorkerRepository,
        codec: ExportFileCodec,
        crypto: CryptoService,
        *,
        max_rows: int = MAX_EXPORT_ROWS,
        lease_seconds: int = 900,
        heartbeat_interval_s: float | None = None,
        on_complete_stage: Callable[[str], None] | None = None,
    ) -> None:
        if lease_seconds < 3:
            raise ValueError("export lease must be at least 3 seconds")
        self.repository = repository
        self.codec = codec
        self.crypto = crypto
        self.max_rows = max_rows
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_s = heartbeat_interval_s or lease_seconds / 3
        self.on_complete_stage = on_complete_stage

    def _phone(self, claim: ExportClaim, row: dict[str, object]) -> str:
        if not claim.decrypted:
            return str(row["phone_mask"])
        payload = row["phone_enc"]
        phone_hmac = row["phone_hmac"]
        version = row["key_version"]
        if (
            not isinstance(payload, bytes)
            or not isinstance(phone_hmac, str)
            or not isinstance(version, int)
            or isinstance(version, bool)
        ):
            raise ValueError("invalid protected export phone")
        table = "unmatched_report" if claim.filters.dataset == "unmatched" else "sms_message"
        return self.crypto.decrypt_phone(payload, version, phone_hmac.strip(), table=table)

    async def process(self, task_id: int) -> int:
        claim = await self.repository.claim(
            task_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return 0
        count = 0
        completed_path: Path | None = None
        created_here = False
        stopped = asyncio.Event()
        lease_lost = asyncio.Event()

        reusable = self.codec.find_reusable_final(claim.id)
        if reusable is not None and reusable.row_count is not None:
            if self.on_complete_stage is not None:
                self.on_complete_stage("before_mark_done")
            await self.repository.mark_done(
                claim.id,
                lease_id=claim.lease_id,
                file_path=str(reusable.path),
                row_count=reusable.row_count,
            )
            return reusable.row_count

        async def csv_rows() -> AsyncIterator[tuple[str, ...]]:
            nonlocal count
            async for row in self.repository.rows(claim):
                count += 1
                if count > self.max_rows:
                    raise ExportTooLarge("导出结果超过 100000 行")
                if claim.filters.dataset == "unmatched":
                    yield (
                        _text(row["created_at"]),
                        _text(row["custom_id"]),
                        _text(row["vendor_task_id"]),
                        self._phone(claim, row),
                        _text(row["report_status"]),
                        _text(row["report_desc"]),
                        _text(row["report_time"]),
                    )
                else:
                    yield (
                        _text(row["created_at"]),
                        _text(row["batch_no"]),
                        _text(row["category"]),
                        self._phone(claim, row),
                        _text(row["status"]),
                        _text(row["report_desc"]),
                        _text(row["report_time"]),
                        decrypt_batch_display_content(
                            self.crypto,
                            row["display_content_enc"],
                            _text(row["batch_no"]),
                        ),
                    )

        async def maintain_lease() -> None:
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(
                        stopped.wait(),
                        timeout=self.heartbeat_interval_s,
                    )
                    return
                except TimeoutError:
                    pass
                try:
                    renewed = await self.repository.heartbeat(
                        claim.id,
                        claim.lease_id,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_task = asyncio.create_task(maintain_lease())
        build_task: asyncio.Task[Path] | None = None
        lost_task = asyncio.create_task(lease_lost.wait())
        try:
            header = (
                UNMATCHED_CSV_HEADER
                if claim.filters.dataset == "unmatched"
                else CSV_HEADER
            )
            build_task = asyncio.create_task(
                self.codec.write_csv(
                    claim.id,
                    claim.lease_id,
                    header,
                    csv_rows(),
                )
            )
            done, _pending = await asyncio.wait(
                {build_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_task in done and lease_lost.is_set():
                build_task.cancel()
                with suppress(asyncio.CancelledError):
                    await build_task
                raise ExportLeaseLost("export lease lost during file generation")
            completed_path = await build_task
            created_here = True
            if lease_lost.is_set():
                raise ExportLeaseLost("export lease lost before completion")
            proof = self.codec.verify_ready(
                completed_path,
                expected_task_id=claim.id,
                expected_lease_id=claim.lease_id,
                require_manifest=True,
            )
            if proof.row_count != count:
                raise RuntimeError("export row count mismatch")
            if self.on_complete_stage is not None:
                self.on_complete_stage("before_mark_done")
            await self.repository.mark_done(
                claim.id,
                lease_id=claim.lease_id,
                file_path=str(completed_path),
                row_count=count,
            )
            return count
        except Exception as error:
            if created_here and completed_path is not None:
                self.codec.remove(completed_path)
            if not isinstance(error, ExportLeaseLost):
                with suppress(Exception):
                    await self.repository.mark_failed(
                        claim.id,
                        lease_id=claim.lease_id,
                    )
            raise
        finally:
            stopped.set()
            lost_task.cancel()
            heartbeat_task.cancel()
            if build_task is not None and not build_task.done():
                build_task.cancel()
                with suppress(asyncio.CancelledError):
                    await build_task
            with suppress(asyncio.CancelledError):
                await lost_task
            with suppress(asyncio.CancelledError):
                await heartbeat_task
