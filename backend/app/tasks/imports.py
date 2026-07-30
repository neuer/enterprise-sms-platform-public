"""加密导入源的分块解析、批量写入与崩溃恢复投递。"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from redis.asyncio import Redis

from app.core.bounded_executor import run_bounded
from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.blacklist import RedisBlacklistCache
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.crypto import CryptoService
from app.services.import_file import ImportFileCodec
from app.services.import_repository import ImportParseClaim, SqlImportRepository
from app.services.imports import (
    ImportFormatError,
    ImportLimits,
    ImportParseChunk,
    ImportParser,
    ImportTooLarge,
    RemovedPhone,
)
from app.services.runtime_policy import SqlRuntimePolicyLoader
from app.settings import get_settings
from app.tasks import celery_app


class _Blacklist:
    def __init__(self, redis: Any, settings: Any) -> None:
        self.cache = RedisBlacklistCache(redis)
        self.repository = SqlBlacklistRepository(settings)

    async def matches(self, candidates: set[str]) -> set[str]:
        return await self.cache.matches(candidates, self.repository.all_hmacs)


def _next_chunk(iterator: Iterator[ImportParseChunk]) -> ImportParseChunk | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _write_removed(
    root: Path,
    claim: ImportParseClaim,
    removed: list[RemovedPhone],
) -> str | None:
    if not removed:
        return None
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    filename = f"removed-{claim.import_id}-{claim.lease_id}.csv"
    path = root / filename
    try:
        with path.open("x", encoding="utf-8", newline="") as output:
            os.chmod(path, 0o600)
            writer = csv.writer(output)
            writer.writerow(["phone_mask", "source_row", "reason"])
            writer.writerows((item.phone_mask, item.source_row, item.reason) for item in removed)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return filename


async def _discard_failed_source(
    repository: SqlImportRepository,
    codec: ImportFileCodec,
    claim: ImportParseClaim,
) -> None:
    """仅在本租约成功固化失败状态后删除密文源并清空文件引用。"""

    try:
        await run_bounded(codec.remove, claim.source_file, timeout_s=5)
    except OSError:
        return
    await repository.clear_source(claim)


async def process_import_once(import_id: str) -> int:
    settings = get_settings()
    repository = SqlImportRepository(settings)
    claim = await repository.claim_parse(import_id)
    if claim is None:
        return 0
    crypto = CryptoService.from_settings(settings)
    policy = await SqlRuntimePolicyLoader(settings, task_safe=True).load()
    limits = ImportLimits.from_policy(policy)
    codec = ImportFileCodec(crypto, settings.import_storage_dir)
    redis: Any = Redis.from_url(settings.redis_control_url, decode_responses=True)
    temporary: Any = None
    removed: list[RemovedPhone] = []
    removed_file: str | None = None
    valid_count = 0
    invalid_count = 0
    duplicate_count = 0
    blacklisted_count = 0
    try:
        temporary = await run_bounded(
            codec.decrypt_to_memory,
            claim.source_file,
            expected_size=claim.source_size,
            max_bytes=limits.max_bytes,
            timeout_s=30,
        )
        parser = ImportParser(
            crypto,
            _Blacklist(redis, settings),
            limits=limits,
        )
        chunks = parser.iter_chunks(
            claim.filename,
            temporary,
            size=claim.source_size,
            chunk_size=500,
        )
        while True:
            chunk = await run_bounded(_next_chunk, chunks, timeout_s=30)
            if chunk is None:
                break
            blocked_candidates = await parser.blacklist.matches(
                set().union(*chunk.candidates_by_active.values())
                if chunk.candidates_by_active
                else set()
            )
            blocked_active = {
                active
                for active, candidates in chunk.candidates_by_active.items()
                if not candidates.isdisjoint(blocked_candidates)
            }
            accepted = tuple(item for item in chunk.valid if item.phone_hmac not in blocked_active)
            blocked = [
                RemovedPhone(item.phone_mask, item.source_row, "blacklist")
                for item in chunk.valid
                if item.phone_hmac in blocked_active
            ]
            if not await repository.append_parse_batch(claim, accepted):
                return 0
            valid_count += len(accepted)
            blacklisted_count += len(blocked)
            invalid_count += sum(item.reason == "invalid" for item in chunk.removed)
            duplicate_count += sum(item.reason == "duplicate" for item in chunk.removed)
            removed.extend(chunk.removed)
            removed.extend(blocked)
        removed_file = await run_bounded(
            _write_removed,
            settings.import_storage_dir,
            claim,
            removed,
            timeout_s=10,
        )
        if not await repository.finish_parse(
            claim,
            valid=valid_count,
            invalid=invalid_count,
            duplicate=duplicate_count,
            blacklisted=blacklisted_count,
            invalid_file=removed_file,
        ):
            if removed_file is not None:
                await run_bounded(
                    (settings.import_storage_dir / removed_file).unlink,
                    missing_ok=True,
                    timeout_s=5,
                )
            return 0
        await run_bounded(codec.remove, claim.source_file, timeout_s=5)
        await repository.clear_source(claim)
        return valid_count
    except ImportTooLarge:
        if await repository.fail_parse(claim, "IMPORT_TOO_LARGE"):
            await _discard_failed_source(repository, codec, claim)
        return 0
    except ImportFormatError:
        if await repository.fail_parse(claim, "IMPORT_FORMAT_INVALID"):
            await _discard_failed_source(repository, codec, claim)
        return 0
    except (InvalidTag, OSError, UnicodeError, ValueError):
        if await repository.fail_parse(claim, "IMPORT_PARSE_FAILED"):
            await _discard_failed_source(repository, codec, claim)
        return 0
    except BaseException:
        await repository.release_parse(claim)
        raise
    finally:
        if temporary is not None:
            temporary.close()
        await redis.aclose()


class ImportSender:
    async def send(self, import_id: str) -> None:
        await run_bounded(
            celery_app.send_task,
            "app.tasks.process_import",
            args=[import_id],
            queue="bulk",
            ignore_result=True,
            timeout_s=5,
        )


async def dispatch_imports_once(
    repository: SqlImportRepository,
    sender: ImportSender,
) -> int:
    import_ids = await repository.pending_parse_ids()
    for import_id in import_ids:
        await sender.send(import_id)
    return len(import_ids)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.process_import",
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=120,
    time_limit=150,
)
def process_import(import_id: str) -> int:
    return run_worker_async(process_import_once(import_id))


@celery_app.task(name="app.tasks.dispatch_imports")  # type: ignore[untyped-decorator]
@tracked_job("dispatch_imports", expect_interval_s=30)
def dispatch_imports() -> int:
    return run_worker_async(dispatch_imports_once(SqlImportRepository(), ImportSender()))
