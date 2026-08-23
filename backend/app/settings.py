"""应用配置与 Docker secrets 读取。"""

from __future__ import annotations

import os
import re
import ssl
from collections.abc import Mapping
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote, urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# 与 raw_spill.capture_reservation_bytes 对齐：一次 64MiB 恢复捕获 + 文档化帧开销。
# 不得 import raw_spill（raw_spill → crypto → settings 会成环）。
RAW_SPILL_RECOVERY_CAPTURE_BYTES = 64 * 1024 * 1024
RAW_SPILL_CAPTURE_OVERHEAD_BYTES = 1024 * 1024
RAW_SPILL_MIN_TOTAL_BYTES = (
    RAW_SPILL_RECOVERY_CAPTURE_BYTES + RAW_SPILL_CAPTURE_OVERHEAD_BYTES
)
# 与 deploy/docker-compose.yml worker-realtime mem_limit: 768m、-c 2 对齐。
# 恢复峰值按「并发恢复数 × 2 × 64MiB」（明文 + 再加密）核算，不得大于该预算。
WORKER_RSS_LIMIT_BYTES_DEFAULT = 768 * 1024 * 1024
RAW_SPILL_RECOVER_CONCURRENCY_DEFAULT = 2
RAW_SPILL_RECOVER_FILE_RSS_BYTES = 2 * RAW_SPILL_RECOVERY_CAPTURE_BYTES
RAW_SPILL_RECOVER_MAX_FILES_DEFAULT = 8
RAW_SPILL_RECOVER_MAX_SECONDS_DEFAULT = 8.0

DatabaseRole = Literal[
    "auth",
    "accept",
    "send",
    "callback",
    "export",
    "scheduler",
    "metrics",
]
DATABASE_ROLE_USERS: dict[str, str] = {
    "auth": "sms_auth",
    "accept": "sms_accept",
    "send": "sms_send",
    "callback": "sms_callback",
    "export": "sms_export",
    "scheduler": "sms_scheduler",
    "metrics": "sms_metrics",
}


def read_secret_file(path: str | Path) -> str:
    """读取单个 secret，仅移除文件末尾的换行符且不泄露内容。"""

    secret_path = Path(path)
    try:
        value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"secret file unavailable: {secret_path}") from exc
    if not value:
        raise RuntimeError(f"secret file is empty: {secret_path}")
    return value


class Settings(BaseSettings):
    """运行配置；凭据字段只保存挂载文件路径。"""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
    )

    environment: Literal["development", "test", "production"]
    sms_component: Literal["api", "worker", "beat", "background", "migrate", "cli"] = "api"
    debug: bool = False
    auth_mock: bool = False
    vendor_mock: bool = False
    vendor_base_url: str = "http://vendor-mock:9028"
    vendor_live_test_origin: str = "https://vendor.example.invalid"

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "sms_platform"
    db_runtime_role: DatabaseRole = "accept"
    db_owner_password_file: Path = Path("/run/secrets/db_owner_password")
    db_auth_password_file: Path = Path("/run/secrets/db_auth_password")
    db_accept_password_file: Path = Path("/run/secrets/db_accept_password")
    db_send_password_file: Path = Path("/run/secrets/db_send_password")
    db_callback_password_file: Path = Path("/run/secrets/db_callback_password")
    db_export_password_file: Path = Path("/run/secrets/db_export_password")
    db_scheduler_password_file: Path = Path("/run/secrets/db_scheduler_password")
    db_metrics_password_file: Path = Path("/run/secrets/db_metrics_password")
    redis_ha_mode: Literal["standalone", "managed"] = "standalone"
    redis_broker_host: str = "redis"
    redis_broker_port: int = 6379
    redis_broker_db: int = 0
    redis_broker_password_file: Path = Path("/run/secrets/redis_broker_password")
    redis_auth_host: str = "redis-auth"
    redis_auth_port: int = 6379
    redis_auth_db: int = 0
    redis_auth_password_file: Path = Path("/run/secrets/redis_auth_password")
    redis_control_host: str = "redis-control"
    redis_control_port: int = 6379
    redis_control_db: int = 0
    redis_control_password_file: Path = Path("/run/secrets/redis_control_password")
    redis_ca_certs_file: Path | None = None
    db_api_pool_size: int = 8
    db_api_max_overflow: int = 2
    db_worker_pool_size: int = 3
    db_worker_max_overflow: int = 1
    db_beat_pool_size: int = 2
    db_beat_max_overflow: int = 0
    db_metrics_pool_size: int = 2
    db_metrics_max_overflow: int = 0
    db_background_pool_size: int = 2
    db_background_max_overflow: int = 0
    db_pool_timeout_seconds: float = 3.0
    db_connect_timeout_seconds: float = 3.0
    db_api_statement_timeout_ms: int = 15_000
    db_worker_statement_timeout_ms: int = 30_000
    db_beat_statement_timeout_ms: int = 10_000
    db_metrics_statement_timeout_ms: int = 2_000
    db_background_statement_timeout_ms: int = 30_000
    readiness_timeout_seconds: float = 2.0
    readiness_queue_timeout_seconds: float = 0.1
    readiness_max_concurrency: int = 2
    readiness_future_months: int = 3
    metrics_allowed_cidrs: str = "127.0.0.1/32,::1/128"
    metrics_collection_timeout_seconds: float = 2.0
    metrics_snapshot_ttl_seconds: float = 15.0
    import_storage_dir: Path = Path("/var/lib/sms/imports")
    export_storage_dir: Path = Path("/var/lib/sms/exports")
    raw_spill_dir: Path = Path("/var/lib/sms/raw-spill")
    raw_spill_max_total_bytes: int = 512 * 1024 * 1024
    raw_spill_max_pending_files: int = 32
    worker_rss_limit_bytes: int = WORKER_RSS_LIMIT_BYTES_DEFAULT
    raw_spill_recover_concurrency: int = RAW_SPILL_RECOVER_CONCURRENCY_DEFAULT
    raw_spill_recover_max_files: int = RAW_SPILL_RECOVER_MAX_FILES_DEFAULT
    raw_spill_recover_max_plaintext_bytes: int = RAW_SPILL_RECOVERY_CAPTURE_BYTES
    raw_spill_recover_max_seconds: float = RAW_SPILL_RECOVER_MAX_SECONDS_DEFAULT
    security_daily_control_dir: Path = Path("/run/security-report")
    security_daily_config_dir: Path = Path("/run/security-report-config")

    vendor_secret_name_file: Path = Path("/run/secrets/vendor_secret_name")
    vendor_secret_key_file: Path = Path("/run/secrets/vendor_secret_key")
    data_aes_key_file: Path = Path("/run/secrets/data_aes_key")
    data_hmac_key_file: Path = Path("/run/secrets/data_hmac_key")
    audit_context_key_file: Path = Path("/run/secrets/audit_context_key")
    audit_system_api_context_key_file: Path = Path(
        "/run/secrets/audit_system_api_context_key"
    )
    audit_system_realtime_context_key_file: Path = Path(
        "/run/secrets/audit_system_realtime_context_key"
    )
    audit_system_bulk_context_key_file: Path = Path(
        "/run/secrets/audit_system_bulk_context_key"
    )
    audit_producer_domain: Literal["api", "realtime", "bulk"] | None = None
    alert_credential_public_key_file: Path = Path(
        "/run/secrets/alert_credential_public_key"
    )
    alert_credential_private_key_file: Path = Path(
        "/run/secrets/alert_credential_private_key"
    )
    jwt_secret_file: Path = Path("/run/secrets/jwt_secret")
    jwt_accept_legacy: bool = False
    trusted_hosts: str = "*"
    ldap_bind_password_file: Path = Path("/run/secrets/ldap_bind_password")
    metrics_scrape_token_file: Path = Path("/run/secrets/metrics_scrape_token")
    ldap_ca_certs_file: Path = Path("/etc/ssl/certs/ca-certificates.crt")
    callback_mtls_cert_file: Path | None = None
    callback_mtls_key_file: Path | None = None
    callback_ca_certs_file: Path | None = None
    callback_egress_allowed_cidrs: str = ""
    callback_egress_allowed_ports: str = ""
    alert_smtp_allowed_hosts: str = "smtp"
    ldap_allowed_hosts: str = ""

    @property
    def vendor_live_test(self) -> bool:
        """开发认证 mock 与真实厂商组合只用于受控联调环境。"""

        return (
            self.environment == "development"
            and self.debug
            and self.auth_mock
            and not self.vendor_mock
        )

    @property
    def is_production(self) -> bool:
        """生产边界只能由显式环境模式决定，禁止再从 DEBUG 反推。"""

        return self.environment == "production"

    @property
    def trusted_host_list(self) -> list[str]:
        """解析 TrustedHost 允许列表；空项忽略。"""

        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def redis_ca_bundle_file(self) -> Path:
        """Redis 默认复用平台 CA；部署可为私有 Redis 单独挂载信任根。"""

        return self.redis_ca_certs_file or self.ldap_ca_certs_file

    @model_validator(mode="after")
    def reject_auth_mock_in_production(self) -> Self:
        """生产环境禁止启用认证 mock。"""

        if self.environment == "production":
            unsafe = {
                "DEBUG": self.debug,
                "AUTH_MOCK": self.auth_mock,
                "VENDOR_MOCK": self.vendor_mock,
            }
            if any(unsafe.values()):
                raise ValueError(
                    "production requires DEBUG=0, AUTH_MOCK=0 and VENDOR_MOCK=0"
                )
            if self.redis_ha_mode != "managed":
                raise ValueError(
                    "production requires managed Redis endpoints for every failure domain"
                )
            if self.jwt_accept_legacy:
                raise ValueError("production forbids JWT_ACCEPT_LEGACY")
            hosts = [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]
            if not hosts or "*" in hosts:
                raise ValueError("production forbids TRUSTED_HOSTS=*")
        else:
            if not self.debug or not self.auth_mock:
                raise ValueError(
                    "development/test requires DEBUG=1 and AUTH_MOCK=1"
                )
            if self.environment == "test" and not self.vendor_mock:
                raise ValueError("test requires VENDOR_MOCK=1")
        if self.is_production:
            if not self.ldap_ca_certs_file.is_file():
                raise ValueError("LDAP_CA_CERTS_FILE must be a readable CA file")
            try:
                with self.ldap_ca_certs_file.open("rb"):
                    pass
            except OSError as exc:
                raise ValueError("LDAP_CA_CERTS_FILE must be a readable CA file") from exc
            redis_ca_file = self.redis_ca_bundle_file
            if not redis_ca_file.is_file():
                raise ValueError("REDIS_CA_CERTS_FILE must be a readable CA file")
            try:
                with redis_ca_file.open("rb"):
                    pass
            except OSError as exc:
                raise ValueError("REDIS_CA_CERTS_FILE must be a readable CA file") from exc
        if (self.callback_mtls_cert_file is None) != (
            self.callback_mtls_key_file is None
        ):
            raise ValueError(
                "CALLBACK_MTLS_CERT_FILE and CALLBACK_MTLS_KEY_FILE must be configured together"
            )
        for label, path in (
            ("CALLBACK_MTLS_CERT_FILE", self.callback_mtls_cert_file),
            ("CALLBACK_MTLS_KEY_FILE", self.callback_mtls_key_file),
            ("CALLBACK_CA_CERTS_FILE", self.callback_ca_certs_file),
        ):
            if path is not None and not path.is_file():
                raise ValueError(f"{label} must be a readable mounted file")
        # 解析部署侧不可变上限；生产空 CIDR 表示默认拒绝全部 callback 出站。
        _ = self.callback_egress_networks
        _ = self.callback_egress_port_set
        live_vendor_url = urlsplit(self.vendor_live_test_origin)
        if (
            live_vendor_url.scheme != "https"
            or not live_vendor_url.hostname
            or live_vendor_url.username is not None
            or live_vendor_url.password is not None
            or live_vendor_url.path not in {"", "/"}
            or bool(live_vendor_url.query)
            or bool(live_vendor_url.fragment)
        ):
            raise ValueError(
                "VENDOR_LIVE_TEST_ORIGIN must be an HTTPS origin without credentials"
            )
        if (
            self.vendor_live_test
            and self.vendor_base_url != self.vendor_live_test_origin
        ):
            raise ValueError(
                "vendor live test requires the configured exact HTTPS origin"
            )
        vendor_url = urlsplit(self.vendor_base_url)
        if (
            vendor_url.scheme not in {"http", "https"}
            or not vendor_url.hostname
            or vendor_url.username is not None
            or vendor_url.password is not None
            or vendor_url.path not in {"", "/"}
            or bool(vendor_url.query)
            or bool(vendor_url.fragment)
        ):
            raise ValueError("VENDOR_BASE_URL must be an HTTP(S) origin without credentials")
        if self.is_production and vendor_url.scheme != "https":
            raise ValueError("VENDOR_BASE_URL must use HTTPS in production")
        if not self.alert_smtp_allowed_host_set:
            raise ValueError("ALERT_SMTP_ALLOWED_HOSTS must contain at least one host")
        redis_hosts = (
            self.redis_broker_host,
            self.redis_auth_host,
            self.redis_control_host,
        )
        redis_ports = (
            self.redis_broker_port,
            self.redis_auth_port,
            self.redis_control_port,
        )
        redis_databases = (
            self.redis_broker_db,
            self.redis_auth_db,
            self.redis_control_db,
        )
        if any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host.casefold())
            is None
            for host in redis_hosts
        ):
            raise ValueError("Redis hosts must be DNS names without credentials")
        if len({host.casefold() for host in redis_hosts}) != 3:
            raise ValueError("Redis broker, auth and control hosts must be distinct")
        if any(not 1 <= value <= 65_535 for value in redis_ports):
            raise ValueError("Redis ports must be between 1 and 65535")
        if any(not 0 <= value <= 15 for value in redis_databases):
            raise ValueError("Redis database numbers must be between 0 and 15")
        pool_sizes = (
            self.db_api_pool_size,
            self.db_worker_pool_size,
            self.db_beat_pool_size,
            self.db_metrics_pool_size,
            self.db_background_pool_size,
        )
        overflows = (
            self.db_api_max_overflow,
            self.db_worker_max_overflow,
            self.db_beat_max_overflow,
            self.db_metrics_max_overflow,
            self.db_background_max_overflow,
        )
        timeouts = (
            self.db_api_statement_timeout_ms,
            self.db_worker_statement_timeout_ms,
            self.db_beat_statement_timeout_ms,
            self.db_metrics_statement_timeout_ms,
            self.db_background_statement_timeout_ms,
        )
        if any(not 1 <= value <= 50 for value in pool_sizes):
            raise ValueError("DB component pool sizes must be between 1 and 50")
        if any(not 0 <= value <= 20 for value in overflows):
            raise ValueError("DB component max overflow must be between 0 and 20")
        if not 0.1 <= self.db_pool_timeout_seconds <= 30:
            raise ValueError("DB_POOL_TIMEOUT_SECONDS must be between 0.1 and 30")
        if not 0.1 <= self.db_connect_timeout_seconds <= 30:
            raise ValueError("DB_CONNECT_TIMEOUT_SECONDS must be between 0.1 and 30")
        if any(not 100 <= value <= 120_000 for value in timeouts):
            raise ValueError("DB statement timeouts must be between 100 and 120000 ms")
        if not 0.1 <= self.readiness_timeout_seconds <= 10:
            raise ValueError("READINESS_TIMEOUT_SECONDS must be between 0.1 and 10")
        if not 0.01 <= self.readiness_queue_timeout_seconds <= 1:
            raise ValueError(
                "READINESS_QUEUE_TIMEOUT_SECONDS must be between 0.01 and 1"
            )
        if not 1 <= self.readiness_max_concurrency <= 8:
            raise ValueError("READINESS_MAX_CONCURRENCY must be between 1 and 8")
        if not 3 <= self.readiness_future_months <= 24:
            raise ValueError("READINESS_FUTURE_MONTHS must be between 3 and 24")
        if not self.metrics_allowed_networks:
            raise ValueError("METRICS_ALLOWED_CIDRS must contain at least one network")
        if not 0.1 <= self.metrics_collection_timeout_seconds <= 10:
            raise ValueError(
                "METRICS_COLLECTION_TIMEOUT_SECONDS must be between 0.1 and 10"
            )
        if not 1 <= self.metrics_snapshot_ttl_seconds <= 60:
            raise ValueError("METRICS_SNAPSHOT_TTL_SECONDS must be between 1 and 60")
        max_spill_bytes = 8 * 1024 * 1024 * 1024
        if not RAW_SPILL_MIN_TOTAL_BYTES <= self.raw_spill_max_total_bytes <= max_spill_bytes:
            raise ValueError(
                "RAW_SPILL_MAX_TOTAL_BYTES must be at least one 64MiB recovery "
                "capture plus documented framing overhead, and at most 8GiB"
            )
        if not 1 <= self.raw_spill_max_pending_files <= 256:
            raise ValueError("RAW_SPILL_MAX_PENDING_FILES must be between 1 and 256")
        if not RAW_SPILL_MIN_TOTAL_BYTES <= self.worker_rss_limit_bytes <= 64 * 1024 * 1024 * 1024:
            raise ValueError(
                "WORKER_RSS_LIMIT_BYTES must be at least one 64MiB recovery "
                "capture plus framing overhead, and at most 64GiB"
            )
        if not 1 <= self.raw_spill_recover_concurrency <= 8:
            raise ValueError("RAW_SPILL_RECOVER_CONCURRENCY must be between 1 and 8")
        recover_peak = self.raw_spill_recover_concurrency * RAW_SPILL_RECOVER_FILE_RSS_BYTES
        if recover_peak > self.worker_rss_limit_bytes:
            raise ValueError(
                "RAW_SPILL_RECOVER_CONCURRENCY times 2×64MiB recover peak "
                "exceeds WORKER_RSS_LIMIT_BYTES"
            )
        if self.raw_spill_max_total_bytes > self.worker_rss_limit_bytes:
            raise ValueError(
                "RAW_SPILL_MAX_TOTAL_BYTES exceeds WORKER_RSS_LIMIT_BYTES; "
                "raise the documented worker memory budget before enlarging the disk backlog"
            )
        if not 1 <= self.raw_spill_recover_max_files <= self.raw_spill_max_pending_files:
            raise ValueError(
                "RAW_SPILL_RECOVER_MAX_FILES must be between 1 and RAW_SPILL_MAX_PENDING_FILES"
            )
        if not (
            1024
            <= self.raw_spill_recover_max_plaintext_bytes
            <= self.raw_spill_max_total_bytes
        ):
            raise ValueError(
                "RAW_SPILL_RECOVER_MAX_PLAINTEXT_BYTES must be between 1KiB "
                "and RAW_SPILL_MAX_TOTAL_BYTES"
            )
        if not 0.1 <= self.raw_spill_recover_max_seconds <= 60:
            raise ValueError("RAW_SPILL_RECOVER_MAX_SECONDS must be between 0.1 and 60")
        return self

    @property
    def database_url(self) -> URL:
        """返回当前容器职责的结构化数据库地址。"""

        return self.database_url_for(self.db_runtime_role)

    def database_url_for(self, role: DatabaseRole) -> URL:
        """为单个职责构造独立 DSN；用户名与 secret 路径不可由输入覆盖。"""

        password_files = {
            "auth": self.db_auth_password_file,
            "accept": self.db_accept_password_file,
            "send": self.db_send_password_file,
            "callback": self.db_callback_password_file,
            "export": self.db_export_password_file,
            "scheduler": self.db_scheduler_password_file,
            "metrics": self.db_metrics_password_file,
        }

        return URL.create(
            drivername="postgresql+asyncpg",
            username=DATABASE_ROLE_USERS[role],
            password=read_secret_file(password_files[role]),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def database_owner_url(self) -> URL:
        """仅供 Alembic 与分区维护入口构造 owner DSN。"""

        return URL.create(
            drivername="postgresql+asyncpg",
            username="sms_owner",
            password=read_secret_file(self.db_owner_password_file),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def redis_broker_url(self) -> str:
        """Celery broker/result 专用 Redis；生产密码仅从 broker secret 读取。"""

        if self.is_production and self.sms_component not in {
            "worker",
            "beat",
            "background",
        }:
            raise RuntimeError("this component is not authorized for the Redis broker")
        return self._redis_url(
            username="sms_broker",
            host=self.redis_broker_host,
            port=self.redis_broker_port,
            database=self.redis_broker_db,
            password_file=self.redis_broker_password_file,
        )

    @property
    def redis_auth_url(self) -> str:
        """登录防爆破、会话撤销与 step-up 专用 Redis。"""

        return self._redis_url(
            username="sms_auth",
            host=self.redis_auth_host,
            port=self.redis_auth_port,
            database=self.redis_auth_db,
            password_file=self.redis_auth_password_file,
        )

    @property
    def redis_control_url(self) -> str:
        """配额、频控、幂等、业务锁与重建投影专用 Redis。"""

        return self._redis_url(
            username="sms_control",
            host=self.redis_control_host,
            port=self.redis_control_port,
            database=self.redis_control_db,
            password_file=self.redis_control_password_file,
        )

    @property
    def redis_tls_options(self) -> dict[str, object] | None:
        """生产 Redis 客户端统一使用受信 CA、证书链与主机名校验。"""

        if not self.is_production:
            return None
        return {
            "ssl_ca_certs": str(self.redis_ca_bundle_file),
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
            "ssl_check_hostname": True,
        }

    def _redis_url(
        self,
        *,
        username: str,
        host: str,
        port: int,
        database: int,
        password_file: Path,
    ) -> str:
        """构造内存中的 Redis DSN；生产缺失 secret 时立即失败。"""

        try:
            password = read_secret_file(password_file)
        except RuntimeError:
            if self.is_production:
                raise
            return f"redis://{host}:{port}/{database}"
        user = quote(username, safe="")
        secret = quote(password, safe="")
        scheme = "rediss" if self.is_production else "redis"
        url = f"{scheme}://{user}:{secret}@{host}:{port}/{database}"
        if not self.is_production:
            return url
        ca_file = quote(str(self.redis_ca_bundle_file), safe="")
        return (
            f"{url}?ssl_ca_certs={ca_file}"
            "&ssl_cert_reqs=required&ssl_check_hostname=true"
        )

    def credential(self, name: str) -> str:
        """按白名单名称读取运行凭据，不接受任意文件路径。"""

        if self.sms_component == "api" and name in {
            "vendor_secret_name",
            "vendor_secret_key",
        }:
            raise RuntimeError("vendor credentials are unavailable to the API component")

        credential_files = {
            "vendor_secret_name": self.vendor_secret_name_file,
            "vendor_secret_key": self.vendor_secret_key_file,
            "data_aes_key": self.data_aes_key_file,
            "data_hmac_key": self.data_hmac_key_file,
            "audit_context_key": self.audit_context_key_file,
            "audit_system_api_context_key": self.audit_system_api_context_key_file,
            "audit_system_realtime_context_key": self.audit_system_realtime_context_key_file,
            "audit_system_bulk_context_key": self.audit_system_bulk_context_key_file,
            "alert_credential_public_key": self.alert_credential_public_key_file,
            "alert_credential_private_key": self.alert_credential_private_key_file,
            "jwt_secret": self.jwt_secret_file,
            "ldap_bind_password": self.ldap_bind_password_file,
            "metrics_scrape_token": self.metrics_scrape_token_file,
        }
        try:
            secret_file = credential_files[name]
        except KeyError:
            raise KeyError(f"unknown credential: {name}") from None
        return read_secret_file(secret_file)

    @property
    def metrics_allowed_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """解析指标抓取源网段；无效配置在启动阶段直接拒绝。"""

        try:
            networks = tuple(
                ip_network(value.strip(), strict=True)
                for value in self.metrics_allowed_cidrs.split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise ValueError("METRICS_ALLOWED_CIDRS contains an invalid network") from exc
        if any(
            network.prefixlen == 0
            or not (network.is_private or network.is_loopback)
            for network in networks
        ):
            raise ValueError("METRICS_ALLOWED_CIDRS must be private or loopback")
        return networks

    @property
    def alert_smtp_allowed_host_set(self) -> frozenset[str]:
        """解析部署侧 SMTP 精确允许列表，运行时配置不得扩大该集合。"""

        hosts = frozenset(
            host.strip().casefold()
            for host in self.alert_smtp_allowed_hosts.replace(";", ",").split(",")
            if host.strip()
        )
        if any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?", host) is None for host in hosts
        ):
            raise ValueError("ALERT_SMTP_ALLOWED_HOSTS contains an invalid host")
        return hosts

    @property
    def callback_egress_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """部署侧 callback 最大网段；管理员运行配置只能进一步收窄。"""

        raw = self.callback_egress_allowed_cidrs.strip()
        if not raw and not self.is_production:
            raw = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
        if not raw:
            return ()
        try:
            networks = tuple(
                ip_network(item.strip(), strict=False)
                for item in raw.split(",")
                if item.strip()
            )
        except ValueError as exc:
            raise ValueError("CALLBACK_EGRESS_ALLOWED_CIDRS contains an invalid network") from exc
        approved_v4 = (
            IPv4Network("10.0.0.0/8"),
            IPv4Network("172.16.0.0/12"),
            IPv4Network("192.168.0.0/16"),
        )
        approved_v6 = (IPv6Network("fc00::/7"),)
        for network in networks:
            if isinstance(network, IPv4Network):
                approved = any(network.subnet_of(root) for root in approved_v4)
            else:
                approved = any(network.subnet_of(root) for root in approved_v6)
            if not approved:
                raise ValueError(
                    "CALLBACK_EGRESS_ALLOWED_CIDRS must contain only approved private subnets"
                )
        return networks

    @property
    def callback_egress_port_set(self) -> frozenset[int]:
        """部署侧 callback 最大端口集合；开发缺省兼容本地 HTTP mock。"""

        raw = self.callback_egress_allowed_ports.strip()
        if not raw:
            raw = "443" if self.is_production else "80,443"
        try:
            ports = frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
        except ValueError as exc:
            raise ValueError("CALLBACK_EGRESS_ALLOWED_PORTS contains an invalid port") from exc
        if not ports or any(port < 1 or port > 65535 for port in ports):
            raise ValueError("CALLBACK_EGRESS_ALLOWED_PORTS contains an invalid port")
        return ports

    @property
    def ldap_allowed_host_set(self) -> frozenset[str]:
        """解析部署侧 LDAP 精确允许主机；空列表表示拒绝真实 LDAP 出站。"""

        hosts = frozenset(
            host.strip().casefold()
            for host in self.ldap_allowed_hosts.replace(";", ",").split(",")
            if host.strip()
        )
        if any(
            re.fullmatch(
                r"[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?(?::\d{1,5})?",
                host,
            )
            is None
            for host in hosts
        ):
            raise ValueError("LDAP_ALLOWED_HOSTS contains an invalid host")
        for host in hosts:
            if ":" in host:
                port = int(host.rsplit(":", 1)[1])
                if not 1 <= port <= 65535:
                    raise ValueError("LDAP_ALLOWED_HOSTS contains an invalid port")
        return hosts


@lru_cache
def get_settings() -> Settings:
    """返回进程级只读配置实例。"""

    reject_unknown_runtime_environment(os.environ)
    return Settings()


def reject_unknown_runtime_environment(environ: Mapping[str, str]) -> None:
    """拒绝安全相关配置名的拼写错误；普通宿主环境变量不参与应用配置。"""

    protected_prefixes = (
        "AUTH_",
        "VENDOR_",
        "DB_",
        "REDIS_",
        "READINESS_",
        "METRICS_",
        "IMPORT_",
        "EXPORT_",
        "LDAP_",
        "ALERT_",
        "CALLBACK_",
        "AUDIT_",
    )
    exact_names = {"ENVIRONMENT", "DEBUG"}
    allowed = {name.upper() for name in Settings.model_fields}
    if environ.get("ENVIRONMENT", "").casefold() == "test":
        allowed.update(
            {
                "VENDOR_UAT_POSTGRES_DSN",
                "EXPORT_AUTH_POSTGRES_DSN",
                "SECURITY_SESSION_POSTGRES_DSN",
                "OUTBOX_POSTGRES_DSN",
            }
        )
    unknown = sorted(
        name
        for name in environ
        if (name in exact_names or name.startswith(protected_prefixes))
        and name not in allowed
    )
    if unknown:
        joined = ", ".join(unknown)
        raise RuntimeError(f"unknown runtime configuration: {joined}")
