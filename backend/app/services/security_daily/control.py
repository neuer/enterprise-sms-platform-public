"""安全日报独立 mailer 的文件控制面。"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from app.services.security_daily.contract import (
    BEARER_IN_TEXT,
    PHONE_IN_TEXT,
    SecurityDailyConfiguration,
    SecurityDailyConfigurationError,
    SecurityDailyDeliveryRequest,
    _parse_shanghai_timestamp,
    validate_resend_api_key,
    validate_resend_recipients,
    validate_security_daily_payload,
)

DeliveryEvidence = Literal["missing", "published", "claimed", "result"]
RequestPublishResult = Literal["published", "already_published", "in_progress"]


class SecurityDailyControlError(RuntimeError):
    """独立安全日报 mailer 控制面不可用。"""


class SecurityDailyConfigurationSuperseded(SecurityDailyControlError):
    """更低版本的配置写入被更高版本文件取代，不得写成已生效。"""


@dataclass(frozen=True, slots=True)
class SecurityDailyControlResult:
    request_id: UUID
    report_date: date
    state: Literal["sent", "failed"]
    completed_at: datetime
    error: str | None = None


class SecurityDailyControl(Protocol):
    async def sync_configuration(self, configuration: SecurityDailyConfiguration) -> None: ...

    async def submit(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None: ...

    async def result(self, request_id: UUID) -> SecurityDailyControlResult | None: ...

    async def inspect_delivery(self, request_id: UUID) -> DeliveryEvidence: ...

    async def published_config_version(self) -> int | None: ...


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileSecurityDailyControl:
    """通过请求目录和独立配置目录与 mailer 交换日报配置和投递请求。"""

    def __init__(self, control_dir: Path, config_dir: Path) -> None:
        self.control_dir = control_dir
        self.config_dir = config_dir
        self.request_dir = control_dir / "requests"
        self.result_dir = control_dir / "results"
        self.config_path = config_dir / "resend.json"

    async def sync_configuration(self, configuration: SecurityDailyConfiguration) -> None:
        await asyncio.to_thread(self._write_configuration, configuration)

    def _read_published_version(self) -> int | None:
        if not self.config_path.is_file():
            return None
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise SecurityDailyControlError("安全日报配置同步失败") from error
        if not isinstance(value, dict):
            raise SecurityDailyControlError("安全日报配置同步失败")
        version = value.get("config_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise SecurityDailyControlError("安全日报配置同步失败")
        return version

    def _write_configuration(self, configuration: SecurityDailyConfiguration) -> None:
        """按版本 CAS 原子同步 UI 配置；每个 Writer 使用独立临时文件。"""

        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        operation_id = uuid4().hex
        temporary = self.config_dir / f".resend.{operation_id}.tmp"
        lock_path = self.config_dir / ".resend.json.lock"
        encoded = json.dumps(
            {
                "api_key": validate_resend_api_key(configuration.api_key),
                "recipients": list(validate_resend_recipients(configuration.recipients)),
                "config_version": configuration.config_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                published = self._read_published_version()
                if published is not None and published > configuration.config_version:
                    raise SecurityDailyConfigurationSuperseded(
                        "安全日报配置版本已被更新版本取代"
                    )
                if published == configuration.config_version:
                    return
                temporary.write_text(encoded, encoding="utf-8")
                os.chmod(temporary, 0o600)
                _fsync_path(temporary)
                os.replace(temporary, self.config_path)
                _fsync_path(self.config_path)
                _fsync_path(self.config_dir)
        except SecurityDailyConfigurationSuperseded:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        except (OSError, SecurityDailyConfigurationError) as error:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise SecurityDailyControlError("安全日报配置同步失败") from error

    async def published_config_version(self) -> int | None:
        return await asyncio.to_thread(self._read_published_version)

    async def submit(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(self._write_request, request, payload)

    def inspect_delivery_sync(self, request_id: UUID) -> DeliveryEvidence:
        if (self.result_dir / f"{request_id}.json").is_file():
            return "result"
        if any(self.request_dir.glob(f".{request_id}.*.processing")):
            return "claimed"
        if (self.request_dir / f"{request_id}.json").is_file():
            return "published"
        return "missing"

    async def inspect_delivery(self, request_id: UUID) -> DeliveryEvidence:
        return await asyncio.to_thread(self.inspect_delivery_sync, request_id)

    def _write_request(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> RequestPublishResult:
        if not self.control_dir.is_dir():
            raise SecurityDailyControlError("安全日报独立投递器未连接")
        self.request_dir.mkdir(mode=0o700, exist_ok=True)
        self.result_dir.mkdir(mode=0o700, exist_ok=True)
        path = self.request_dir / f"{request.request_id}.json"
        operation_id = uuid4().hex
        temporary = self.request_dir / f".{request.request_id}.{operation_id}.tmp"
        lock_path = self.request_dir / f".{request.request_id}.lock"
        delivery_id = request.delivery_id or str(request.request_id)
        body = {
            "request_id": str(request.request_id),
            "report_date": request.report_date.isoformat(),
            "action": request.action,
            "config_version": request.config_version,
            "delivery_id": delivery_id,
            "delivery_generation": request.delivery_generation,
            "payload": validate_security_daily_payload(payload),
        }
        if request.recipient_set_digest:
            body["recipient_set_digest"] = request.recipient_set_digest
        try:
            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                evidence = self.inspect_delivery_sync(request.request_id)
                if evidence == "result":
                    return "already_published"
                if evidence == "claimed":
                    return "in_progress"
                if evidence == "published":
                    return "already_published"
                temporary.write_text(
                    json.dumps(body, ensure_ascii=False), encoding="utf-8"
                )
                os.chmod(temporary, 0o600)
                _fsync_path(temporary)
                os.replace(temporary, path)
                _fsync_path(path)
                _fsync_path(self.request_dir)
                return "published"
        except OSError as error:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            evidence = self.inspect_delivery_sync(request.request_id)
            if evidence == "claimed":
                return "in_progress"
            if evidence in {"published", "result"}:
                return "already_published"
            raise SecurityDailyControlError("安全日报投递请求写入失败") from error

    async def result(self, request_id: UUID) -> SecurityDailyControlResult | None:
        return await asyncio.to_thread(self._read_result, request_id)

    def _read_result(self, request_id: UUID) -> SecurityDailyControlResult | None:
        path = self.result_dir / f"{request_id}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            result = SecurityDailyControlResult(
                request_id=UUID(str(value["request_id"])),
                report_date=date.fromisoformat(str(value["report_date"])),
                state=cast(Literal["sent", "failed"], str(value["state"])),
                completed_at=_parse_shanghai_timestamp(str(value["completed_at"]), "completed_at"),
                error=str(value["error"])[:256] if value.get("error") else None,
            )
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise SecurityDailyControlError("安全日报投递结果无效") from error
        if result.request_id != request_id or result.state not in {"sent", "failed"}:
            raise SecurityDailyControlError("安全日报投递结果不匹配")
        if result.error and (
            PHONE_IN_TEXT.search(result.error) or BEARER_IN_TEXT.search(result.error)
        ):
            raise SecurityDailyControlError("安全日报投递结果包含敏感信息")
        return result
