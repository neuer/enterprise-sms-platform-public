"""登录防爆破阈值的最小策略快照；不解析无关 sys_config。"""

from __future__ import annotations

import hashlib
from asyncio import Lock
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

from sqlalchemy import text

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.observability import (
    observe_guard_db_query,
    observe_policy_cache_hit,
    observe_policy_cache_miss,
    observe_policy_load_failure,
    observe_policy_snapshot_age,
)
from app.core.runtime_resources import database_engine
from app.services.runtime_policy import CONFIG_SPECS, InvalidRuntimePolicy, _positive
from app.settings import Settings, get_settings

AUTH_GUARD_KEYS = (
    "login_fail_limit",
    "login_lock_minutes",
    "login_ip_fail_limit",
    "login_ip_ban_minutes",
)
CACHE_TTL_SECONDS = 15.0
STALE_WINDOW_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class AuthGuardPolicy:
    """四个登录阈值加完整快照版本。"""

    login_fail_limit: int
    login_lock_minutes: int
    login_ip_fail_limit: int
    login_ip_ban_minutes: int
    version: int

    @classmethod
    def from_mapping(cls, supplied: Mapping[str, Any], *, version: int = 1) -> AuthGuardPolicy:
        values = {
            key: str(CONFIG_SPECS[key].default)
            for key in AUTH_GUARD_KEYS
        } | {str(key): str(value) for key, value in supplied.items() if key in AUTH_GUARD_KEYS}
        parsed: dict[str, int] = {}
        for key in AUTH_GUARD_KEYS:
            spec = CONFIG_SPECS[key]
            value = _positive(values, key)
            if spec.minimum is not None and value < spec.minimum:
                raise InvalidRuntimePolicy(f"{key} 不得小于 {spec.minimum}")
            if spec.maximum is not None and value > spec.maximum:
                raise InvalidRuntimePolicy(f"{key} 不得大于 {spec.maximum}")
            parsed[key] = value
        if version < 1:
            raise InvalidRuntimePolicy("auth guard policy version is invalid")
        return cls(
            login_fail_limit=parsed["login_fail_limit"],
            login_lock_minutes=parsed["login_lock_minutes"],
            login_ip_fail_limit=parsed["login_ip_fail_limit"],
            login_ip_ban_minutes=parsed["login_ip_ban_minutes"],
            version=version,
        )

    @classmethod
    def defaults(cls) -> AuthGuardPolicy:
        return cls.from_mapping({})


def _version_from_rows(rows: list[tuple[str, str, Any]]) -> int:
    material = "|".join(f"{key}={value}" for key, value, _updated in sorted(rows))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


class SqlAuthGuardPolicyLoader:
    """只读四个认证阈值；使用 sms_auth 连接，不占用 sms_accept 池。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache_ttl_s: float = CACHE_TTL_SECONDS,
        stale_window_s: float = STALE_WINDOW_SECONDS,
        clock: Any = monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache_ttl_s = cache_ttl_s
        self.stale_window_s = stale_window_s
        self.clock = clock
        self._lock = Lock()
        self._cached: AuthGuardPolicy | None = None
        self._cached_at: float | None = None

    async def load(self) -> AuthGuardPolicy:
        now = float(self.clock())
        cached = self._cached
        cached_at = self._cached_at
        if cached is not None and cached_at is not None and now - cached_at < self.cache_ttl_s:
            observe_policy_cache_hit()
            observe_policy_snapshot_age(now - cached_at)
            return cached
        async with self._lock:
            now = float(self.clock())
            cached = self._cached
            cached_at = self._cached_at
            if (
                cached is not None
                and cached_at is not None
                and now - cached_at < self.cache_ttl_s
            ):
                observe_policy_cache_hit()
                observe_policy_snapshot_age(now - cached_at)
                return cached
            try:
                snapshot = await self._load_fresh()
            except Exception:
                observe_policy_load_failure()
                if (
                    cached is not None
                    and cached_at is not None
                    and now - cached_at <= self.stale_window_s
                ):
                    observe_policy_snapshot_age(now - cached_at)
                    return cached
                raise SessionStateUnavailable("auth guard policy unavailable") from None
            self._cached = snapshot
            self._cached_at = now
            observe_policy_snapshot_age(0.0)
            return snapshot

    async def _load_fresh(self) -> AuthGuardPolicy:
        observe_policy_cache_miss()
        observe_guard_db_query()
        engine = database_engine(self.settings.database_url_for("auth"))
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key,value,updated_at
                        FROM sys_config
                        WHERE key = ANY(CAST(:keys AS text[]))
                        """
                    ),
                    {"keys": list(AUTH_GUARD_KEYS)},
                )
                rows = [
                    (str(row["key"]), str(row["value"]), row["updated_at"])
                    for row in result.mappings()
                ]
        finally:
            await engine.dispose()
        if {key for key, _value, _updated in rows} != set(AUTH_GUARD_KEYS):
            raise InvalidRuntimePolicy("auth guard keys are incomplete")
        return AuthGuardPolicy.from_mapping(
            {key: value for key, value, _updated in rows},
            version=_version_from_rows(rows),
        )
