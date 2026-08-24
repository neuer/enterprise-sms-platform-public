"""安全日报独立 mailer 的文件控制面。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

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


class SecurityDailyControlError(RuntimeError):
    """独立安全日报 mailer 控制面不可用。"""


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

    def _write_configuration(self, configuration: SecurityDailyConfiguration) -> None:
        """原子同步 UI 配置；文件仅挂载给独立 mailer 读取。"""

        temporary = self.config_dir / ".resend.json.tmp"
        try:
            self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "api_key": validate_resend_api_key(configuration.api_key),
                        "recipients": list(
                            validate_resend_recipients(configuration.recipients)
                        ),
                        "config_version": configuration.config_version,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.config_path)
        except (OSError, SecurityDailyConfigurationError) as error:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise SecurityDailyControlError("安全日报配置同步失败") from error

    async def submit(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(self._write_request, request, payload)

    def _write_request(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None:
        if not self.control_dir.is_dir():
            raise SecurityDailyControlError("安全日报独立投递器未连接")
        self.request_dir.mkdir(mode=0o700, exist_ok=True)
        self.result_dir.mkdir(mode=0o700, exist_ok=True)
        path = self.request_dir / f"{request.request_id}.json"
        temp = self.request_dir / f".{request.request_id}.tmp"
        body = {
            "request_id": str(request.request_id),
            "report_date": request.report_date.isoformat(),
            "action": request.action,
            "config_version": request.config_version,
            "payload": validate_security_daily_payload(payload),
        }
        try:
            temp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError as error:
            with suppress(OSError):
                temp.unlink(missing_ok=True)
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
