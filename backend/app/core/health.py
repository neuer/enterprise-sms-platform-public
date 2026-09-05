"""无敏感细节的存活/就绪检查与依赖验证。"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.core.apikey import require_api_key_pepper_keyring
from app.core.audit_context import decode_audit_context_key
from app.core.auth.session_policy_sync import AuthSessionPolicyReconciler
from app.core.bounded_executor import run_bounded
from app.core.runtime_resources import database_engine, redis_client
from app.services.crypto import CryptoService
from app.services.runtime_policy import DEFAULTS, RuntimePolicy
from app.services.sensitive_config import AlertCredentialCipher
from app.settings import Settings

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MIGRATION_HEAD = re.compile(r"[0-9]{4}_[a-z0-9_]+")
PARTITION_PARENTS = ("sms_message", "sms_reply")

AsyncReadinessCheck = Callable[[], Awaitable[None]]


def _add_months(moment: datetime, months: int) -> datetime:
    local = moment.astimezone(SHANGHAI)
    absolute = local.year * 12 + local.month - 1 + months
    year, zero_month = divmod(absolute, 12)
    return datetime(year, zero_month + 1, 1, tzinfo=SHANGHAI)


def required_partition_names(
    now: datetime,
    *,
    future_months: int,
) -> frozenset[str]:
    """返回当前月及未来 N 个月两个关键父表的规范分区名。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("partition readiness clock must be timezone-aware")
    if not 3 <= future_months <= 24:
        raise ValueError("partition readiness window must be between 3 and 24 months")
    return frozenset(
        f"{parent}_{_add_months(now, offset):%Y_%m}"
        for parent in PARTITION_PARENTS
        for offset in range(future_months + 1)
    )


@lru_cache
def expected_migration_head() -> str:
    """从镜像内同版本 Alembic 图读取唯一 head。"""

    backend_root = Path(__file__).resolve().parents[2]
    configuration = Config(str(backend_root / "alembic.ini"))
    head = ScriptDirectory.from_config(configuration).get_current_head()
    if head is None or MIGRATION_HEAD.fullmatch(head) is None:
        raise RuntimeError("application migration head is invalid")
    return head


class DatabaseReadinessCheck:
    """在一个有界连接中验证迁移、运行配置与未来分区。"""

    def __init__(
        self,
        settings: Settings,
        *,
        migration_head: str | None = None,
    ) -> None:
        self.settings = settings
        self.migration_head = migration_head or expected_migration_head()

    async def __call__(self) -> None:
        engine = database_engine(self.settings.database_url, component="api")
        async with engine.connect() as connection:
            actual_head = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            if actual_head != self.migration_head:
                raise RuntimeError("database migration is not ready")

            config_result = await connection.execute(
                text("SELECT key,value FROM sys_config")
            )
            config = {
                str(row["key"]): str(row["value"])
                for row in config_result.mappings()
            }
            if not set(DEFAULTS).issubset(config):
                raise RuntimeError("critical runtime configuration is incomplete")
            RuntimePolicy.from_mapping(config)

            now = await connection.scalar(text("SELECT now()"))
            if not isinstance(now, datetime):
                raise RuntimeError("database clock is unavailable")
            required = required_partition_names(
                now,
                future_months=self.settings.readiness_future_months,
            )
            partition_result = await connection.execute(
                text(
                    """
                    SELECT child.relname
                    FROM pg_inherits
                    JOIN pg_class parent ON parent.oid=inhparent
                    JOIN pg_class child ON child.oid=inhrelid
                    WHERE parent.relname=ANY(CAST(:parents AS text[]))
                    """
                ),
                {"parents": list(PARTITION_PARENTS)},
            )
            existing = {
                str(row["relname"])
                for row in partition_result.mappings()
            }
            if not required.issubset(existing):
                raise RuntimeError("future partitions are incomplete")


class ApiKeyPepperReferenceCheck:
    """引用中的 pepper 版本必须仍在独立 keyring；缺失即失败关闭。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self) -> None:
        ring = require_api_key_pepper_keyring(self.settings)
        engine = database_engine(self.settings.database_url_for("auth"), component="api")
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT DISTINCT version FROM (
                      SELECT api_key_hash_version AS version FROM app
                      UNION ALL
                      SELECT api_key_prev_hash_version FROM app
                    ) versions
                    WHERE version IS NOT NULL
                    """
                )
            )
            for row in result:
                if int(row[0]) not in ring.keys:
                    raise RuntimeError("api key pepper version is unavailable")
        await self._assert_digest_inventory()

    async def _assert_digest_inventory(self) -> None:
        from app.core.apikey import (
            API_KEY_ALGORITHMS,
            UnknownDigestAlgorithmError,
            load_legacy_data_hmac_pepper,
            parse_unclassified_algorithms,
        )

        engine = database_engine(self.settings.database_url_for("auth"), component="api")
        async with engine.connect() as target:
            inventory = await target.execute(
                text(
                    """
                    SELECT
                      api_key_hash_algorithm AS algorithm,
                      status
                    FROM app
                    UNION ALL
                    SELECT
                      api_key_prev_hash_algorithm,
                      status
                    FROM app
                    WHERE api_key_prev_hash IS NOT NULL
                    """
                )
            )
            unclassified_active = 0
            needs_legacy = False
            for row in inventory.mappings():
                algorithm = row["algorithm"]
                if algorithm is None:
                    if int(row["status"]) == 1:
                        unclassified_active += 1
                    continue
                if str(algorithm) not in API_KEY_ALGORITHMS:
                    raise RuntimeError("api key digest algorithm is unavailable")
                if str(algorithm) == "legacy_data_hmac_pepper_v1":
                    needs_legacy = True
            policy_row = await target.execute(
                text(
                    """
                    SELECT value FROM sys_config
                    WHERE key='api_key_unclassified_algorithms'
                    """
                )
            )
            policy_value = policy_row.scalar_one_or_none()
            try:
                policy = parse_unclassified_algorithms(
                    None if policy_value is None else str(policy_value)
                )
            except UnknownDigestAlgorithmError as exc:
                raise RuntimeError("api key digest algorithm is unavailable") from exc
            if "legacy_data_hmac_pepper_v1" in policy:
                needs_legacy = True
            if needs_legacy and load_legacy_data_hmac_pepper(self.settings) is None:
                raise RuntimeError("api key legacy pepper is unavailable")
            if unclassified_active and not policy and self.settings.is_production:
                raise RuntimeError("unclassified api key digests are present")


class AuthSessionPolicyReadinessCheck:
    """PostgreSQL 权威策略与 Redis 快照必须可收敛；超前或冲突失败关闭。"""

    def __init__(
        self,
        settings: Settings,
        *,
        reconciler: AuthSessionPolicyReconciler | None = None,
    ) -> None:
        self.reconciler = reconciler or AuthSessionPolicyReconciler(settings)

    async def __call__(self) -> None:
        await self.reconciler.ensure_ready()


class RedisReadinessCheck:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self) -> None:
        for endpoint in (
            self.settings.redis_auth_url,
            self.settings.redis_control_url,
        ):
            client = redis_client(endpoint)
            if await client.ping() is not True:
                raise RuntimeError("required Redis failure domain is unavailable")


def _validate_runtime_secrets(settings: Settings) -> None:
    """读取并解析必要运行密钥；值和派生信息不得离开本函数。"""

    _ = settings.database_url
    _ = settings.database_url_for("auth")
    secrets = {
        name: settings.credential(name)
        for name in (
            "data_aes_key",
            "data_hmac_key",
            "api_key_pepper_key",
            "audit_context_key",
            "audit_system_api_context_key",
            "alert_credential_public_key",
            "jwt_secret",
            "ldap_bind_password",
        )
    }
    CryptoService.from_secret_values(
        secrets["data_aes_key"],
        secrets["data_hmac_key"],
    )
    require_api_key_pepper_keyring(settings)
    principal_audit_key = decode_audit_context_key(secrets["audit_context_key"])
    api_system_audit_key = decode_audit_context_key(
        secrets["audit_system_api_context_key"]
    )
    if hmac.compare_digest(principal_audit_key, api_system_audit_key):
        raise RuntimeError("audit context keys must be independent")
    AlertCredentialCipher.from_public_file(
        settings.alert_credential_public_key_file
    )


class RuntimeSecretReadinessCheck:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self) -> None:
        await run_bounded(
            _validate_runtime_secrets,
            self.settings,
            timeout_s=self.settings.readiness_timeout_seconds,
        )


class ReadinessProbe:
    """以总超时和并发闸门执行必要检查，失败只返回布尔状态。"""

    def __init__(
        self,
        checks: Sequence[AsyncReadinessCheck],
        *,
        timeout_seconds: float,
        queue_timeout_seconds: float,
        max_concurrency: int,
    ) -> None:
        if not checks:
            raise ValueError("readiness checks cannot be empty")
        if not 0.1 <= timeout_seconds <= 10:
            raise ValueError("readiness timeout is outside the safe range")
        if not 0.01 <= queue_timeout_seconds <= 1:
            raise ValueError("readiness queue timeout is outside the safe range")
        if not 1 <= max_concurrency <= 8:
            raise ValueError("readiness concurrency is outside the safe range")
        self.checks = tuple(checks)
        self.timeout_seconds = timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def ready(self) -> bool:
        try:
            await asyncio.wait_for(
                self.semaphore.acquire(),
                timeout=self.queue_timeout_seconds,
            )
        except TimeoutError:
            return False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                for check in self.checks:
                    await check()
            return True
        except Exception as error:
            LOGGER.warning(
                "readiness_probe_failed",
                extra={"error_type": type(error).__name__},
            )
            return False
        finally:
            self.semaphore.release()


def create_readiness_probe(
    settings: Settings,
    *,
    startup_check: AsyncReadinessCheck,
) -> ReadinessProbe:
    return ReadinessProbe(
        (
            RuntimeSecretReadinessCheck(settings),
            DatabaseReadinessCheck(settings),
            ApiKeyPepperReferenceCheck(settings),
            RedisReadinessCheck(settings),
            AuthSessionPolicyReadinessCheck(settings),
            startup_check,
        ),
        timeout_seconds=settings.readiness_timeout_seconds,
        queue_timeout_seconds=settings.readiness_queue_timeout_seconds,
        max_concurrency=settings.readiness_max_concurrency,
    )
