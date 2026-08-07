from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.engine import URL


def load_settings_module() -> ModuleType:
    assert importlib.util.find_spec("app.settings") is not None, "app.settings 尚未实现"
    return importlib.import_module("app.settings")


def test_secret_reader_strips_only_line_endings(tmp_path: Path) -> None:
    module = load_settings_module()
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(b"  value with spaces  \r\n")

    assert module.read_secret_file(secret_file) == "  value with spaces  "


def test_secret_reader_rejects_missing_or_empty_file(tmp_path: Path) -> None:
    module = load_settings_module()
    empty_file = tmp_path / "empty"
    empty_file.write_text("\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="secret file"):
        module.read_secret_file(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="secret file"):
        module.read_secret_file(empty_file)


def test_production_rejects_auth_mock() -> None:
    module = load_settings_module()

    with pytest.raises(ValueError, match="AUTH_MOCK"):
        module.Settings(
            _env_file=None,
            environment="production",
            debug=False,
            auth_mock=True,
        )


def test_environment_is_required_and_modes_reject_unsafe_combinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_settings_module()

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(ValueError, match="environment"):
        module.Settings(
            _env_file=None,
            debug=True,
            auth_mock=True,
            vendor_mock=True,
        )
    with pytest.raises(ValueError, match="production"):
        module.Settings(
            _env_file=None,
            environment="production",
            debug=True,
            auth_mock=False,
            vendor_mock=False,
        )
    with pytest.raises(ValueError, match="VENDOR_MOCK"):
        module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=False,
            vendor_base_url="https://vendor.example.invalid",
        )


def test_unknown_security_related_environment_variable_is_rejected() -> None:
    module = load_settings_module()

    with pytest.raises(RuntimeError, match="VENDOR_MOKC"):
        module.reject_unknown_runtime_environment(
            {
                "ENVIRONMENT": "production",
                "VENDOR_MOKC": "0",
                "PATH": "/usr/bin",
            }
        )
    module.reject_unknown_runtime_environment(
        {
            "ENVIRONMENT": "test",
            "VENDOR_MOCK": "1",
            "VENDOR_UAT_POSTGRES_DSN": "postgresql://test",
            "EXPORT_AUTH_POSTGRES_DSN": "postgresql://test",
            "SECURITY_SESSION_POSTGRES_DSN": "postgresql://test",
            "OUTBOX_POSTGRES_DSN": "postgresql://test",
            "PATH": "/usr/bin",
        }
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("db_api_pool_size", 0, "pool sizes"),
        ("db_worker_max_overflow", 21, "max overflow"),
        ("db_pool_timeout_seconds", 0, "POOL_TIMEOUT"),
        ("db_connect_timeout_seconds", 31, "CONNECT_TIMEOUT"),
        ("db_metrics_statement_timeout_ms", 99, "statement timeouts"),
    ],
)
def test_database_pool_budgets_are_bounded(
    field: str,
    value: int,
    message: str,
) -> None:
    module = load_settings_module()

    with pytest.raises(ValueError, match=message):
        module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=True,
            vendor_base_url="http://vendor-mock:9028",
            **{field: value},
        )


def test_metrics_source_allowlist_rejects_public_or_all_address_networks() -> None:
    module = load_settings_module()

    public_network = "8.8." + "8.0/24"
    for cidr in ("0.0.0.0/0", "::/0", public_network):
        with pytest.raises(ValueError, match="private or loopback"):
            module.Settings(
                _env_file=None,
                environment="test",
                debug=True,
                auth_mock=True,
                vendor_mock=True,
                metrics_allowed_cidrs=cidr,
            )


def test_ldap_allowed_hosts_parse_and_reject_invalid_entries() -> None:
    module = load_settings_module()
    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        vendor_base_url="http://vendor-mock:9028",
        ldap_allowed_hosts="dc01.example.com:636;dc02.example.com:636",
    )
    assert settings.ldap_allowed_host_set == frozenset(
        {"dc01.example.com:636", "dc02.example.com:636"}
    )
    with pytest.raises(ValueError, match="LDAP_ALLOWED_HOSTS"):
        invalid = module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=True,
            vendor_base_url="http://vendor-mock:9028",
            ldap_allowed_hosts="bad host",
        )
        _ = invalid.ldap_allowed_host_set


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("readiness_timeout_seconds", 0.09, "READINESS_TIMEOUT"),
        ("readiness_queue_timeout_seconds", 1.01, "READINESS_QUEUE_TIMEOUT"),
        ("readiness_max_concurrency", 0, "READINESS_MAX_CONCURRENCY"),
        ("readiness_future_months", 2, "READINESS_FUTURE_MONTHS"),
    ],
)
def test_readiness_budgets_are_bounded(
    field: str,
    value: float,
    message: str,
) -> None:
    module = load_settings_module()

    with pytest.raises(ValueError, match=message):
        module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=True,
            vendor_base_url="http://vendor-mock:9028",
            **{field: value},
        )


def test_production_vendor_endpoint_requires_https(tmp_path: Path) -> None:
    module = load_settings_module()
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")

    with pytest.raises(ValueError, match="VENDOR_BASE_URL"):
        module.Settings(
            _env_file=None,
            environment="production",
            debug=False,
            auth_mock=False,
            vendor_mock=False,
            redis_ha_mode="managed",
            ldap_ca_certs_file=ca_file,
            vendor_base_url="http://vendor.example.test",
        )

    settings = module.Settings(
        _env_file=None,
        environment="production",
        debug=False,
        auth_mock=False,
        vendor_mock=False,
        redis_ha_mode="managed",
        ldap_ca_certs_file=ca_file,
        vendor_base_url="https://vendor.example.test",
    )
    assert settings.vendor_base_url == "https://vendor.example.test"


@pytest.mark.parametrize(
    "vendor_base_url",
    (
        "https://vendor.example.test/path",
        "https://vendor.example.test?probe=1",
        "https://vendor.example.test#fragment",
    ),
)
def test_vendor_endpoint_rejects_non_origin_components(
    tmp_path: Path,
    vendor_base_url: str,
) -> None:
    module = load_settings_module()
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")

    with pytest.raises(ValueError, match="HTTP\\(S\\) origin"):
        module.Settings(
            _env_file=None,
            environment="production",
            debug=False,
            auth_mock=False,
            vendor_mock=False,
            redis_ha_mode="managed",
            ldap_ca_certs_file=ca_file,
            vendor_base_url=vendor_base_url,
        )


def test_live_test_mode_requires_exact_https_vendor_origin_without_host_allowlist(
    tmp_path: Path,
) -> None:
    module = load_settings_module()

    settings = module.Settings(
        _env_file=None,
        environment="development",
        debug=True,
        auth_mock=True,
        vendor_mock=False,
        vendor_base_url="https://vendor.example.invalid",
    )

    assert settings.vendor_live_test is True
    for invalid_origin in (
        "http://vendor.example.invalid",
        "https://user@vendor.example.invalid",
        "https://vendor.example.invalid/path",
        "https://vendor.example.invalid?probe=1",
        "https://other.example.test",
    ):
        with pytest.raises(ValueError, match="live test"):
            module.Settings(
                _env_file=None,
                environment="development",
                debug=True,
                auth_mock=True,
                vendor_mock=False,
                vendor_base_url=invalid_origin,
            )

    source = (Path(module.__file__)).read_text(encoding="utf-8")
    assert "VENDOR_TEST_ALLOWLIST_FILE" not in source


def test_live_test_origin_can_be_supplied_by_local_private_configuration() -> None:
    module = load_settings_module()

    settings = module.Settings(
        _env_file=None,
        environment="development",
        debug=True,
        auth_mock=True,
        vendor_mock=False,
        vendor_base_url="https://gateway.example.test",
        vendor_live_test_origin="https://gateway.example.test",
    )

    assert settings.vendor_live_test is True
    assert settings.vendor_live_test_origin == "https://gateway.example.test"


def test_mock_mode_does_not_require_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_settings_module()
    monkeypatch.setattr(
        module,
        "VENDOR_TEST_ALLOWLIST_FILE",
        tmp_path / "missing.json",
        raising=False,
    )

    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        vendor_base_url="http://vendor-mock:9028",
    )

    assert settings.vendor_live_test is False


def test_smtp_allowed_hosts_are_deployment_owned_and_exact() -> None:
    module = load_settings_module()
    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        alert_smtp_allowed_hosts="smtp, mail.internal;MAIL-BACKUP.INTERNAL",
    )

    assert settings.alert_smtp_allowed_host_set == {
        "smtp",
        "mail.internal",
        "mail-backup.internal",
    }


def test_production_ldap_requires_readable_ca_while_debug_mock_does_not(
    tmp_path: Path,
) -> None:
    module = load_settings_module()
    missing = tmp_path / "missing-ca.pem"
    with pytest.raises(ValueError, match="LDAP_CA_CERTS_FILE"):
        module.Settings(
            _env_file=None,
            environment="production",
            debug=False,
            auth_mock=False,
            vendor_mock=False,
            redis_ha_mode="managed",
            ldap_ca_certs_file=missing,
        )
    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        ldap_ca_certs_file=missing,
    )
    assert settings.auth_mock is True


def test_callback_tls_files_are_optional_paired_and_readable(tmp_path: Path) -> None:
    module = load_settings_module()
    cert_file = tmp_path / "client-cert.pem"
    key_file = tmp_path / "client-key.pem"
    ca_file = tmp_path / "callback-ca.pem"
    for path in (cert_file, key_file, ca_file):
        path.write_text("test-only", encoding="utf-8")

    with pytest.raises(ValueError, match="configured together"):
        module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=True,
            callback_mtls_cert_file=cert_file,
        )
    with pytest.raises(ValueError, match="CALLBACK_CA_CERTS_FILE"):
        module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=True,
            callback_ca_certs_file=tmp_path / "missing-ca.pem",
        )

    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        callback_mtls_cert_file=cert_file,
        callback_mtls_key_file=key_file,
        callback_ca_certs_file=ca_file,
    )
    assert settings.callback_mtls_cert_file == cert_file
    assert settings.callback_mtls_key_file == key_file
    assert settings.callback_ca_certs_file == ca_file


def test_redis_failure_domains_use_distinct_secret_backed_endpoints(
    tmp_path: Path,
) -> None:
    module = load_settings_module()
    broker = tmp_path / "broker"
    auth = tmp_path / "auth"
    control = tmp_path / "control"
    broker.write_text("broker-pass", encoding="utf-8")
    auth.write_text("auth/pass", encoding="utf-8")
    control.write_text("control-pass", encoding="utf-8")

    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        redis_broker_password_file=broker,
        redis_auth_password_file=auth,
        redis_control_password_file=control,
    )

    assert settings.redis_broker_url == "redis://sms_broker:broker-pass@redis:6379/0"
    assert settings.redis_auth_url == (
        "redis://sms_auth:auth%2Fpass@redis-auth:6379/0"
    )
    assert settings.redis_control_url == (
        "redis://sms_control:control-pass@redis-control:6379/0"
    )
    assert len(
        {
            settings.redis_broker_url,
            settings.redis_auth_url,
            settings.redis_control_url,
        }
    ) == 3


def test_redis_domains_reject_single_host_and_production_standalone(
    tmp_path: Path,
) -> None:
    module = load_settings_module()
    with pytest.raises(ValueError, match="must be distinct"):
        module.Settings(
            _env_file=None,
            environment="test",
            debug=True,
            auth_mock=True,
            vendor_mock=True,
            redis_auth_host="redis",
        )

    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")
    with pytest.raises(ValueError, match="managed Redis"):
        module.Settings(
            _env_file=None,
            environment="production",
            debug=False,
            auth_mock=False,
            vendor_mock=False,
            ldap_ca_certs_file=ca_file,
            vendor_base_url="https://vendor.example.test",
        )


def test_ldap_connection_config_and_bootstrap_users_are_not_environment_settings() -> None:
    module = load_settings_module()

    retired = {
        "bootstrap_admin_users",
        "ldap_server",
        "ldap_base_dn",
        "ldap_bind_dn",
        "ldap_user_search_filter",
        "ldap_connect_timeout_s",
        "ldap_receive_timeout_s",
    }

    assert retired.isdisjoint(module.Settings.model_fields)
    assert {"auth_mock", "ldap_bind_password_file", "ldap_ca_certs_file"}.issubset(
        module.Settings.model_fields
    )


def test_database_url_is_built_with_url_create(tmp_path: Path) -> None:
    module = load_settings_module()
    password_file = tmp_path / "db_accept_password"
    password_file.write_text("p@ss:/?#\n", encoding="utf-8")
    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        db_host="postgres.internal",
        db_port=5433,
        db_name="sms_test",
        db_runtime_role="accept",
        db_accept_password_file=password_file,
    )

    url = settings.database_url

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "sms_accept"
    assert url.password == "p@ss:/?#"
    assert url.host == "postgres.internal"
    assert url.port == 5433
    assert url.database == "sms_test"
    assert "p@ss:/?#" not in str(url)


def test_each_database_role_uses_fixed_user_and_its_own_secret(tmp_path: Path) -> None:
    module = load_settings_module()
    password_fields: dict[str, Path] = {}
    for role in module.DATABASE_ROLE_USERS:
        secret = tmp_path / f"db_{role}_password"
        secret.write_text(f"{role}-secret\n", encoding="utf-8")
        password_fields[f"db_{role}_password_file"] = secret
    owner_secret = tmp_path / "db_owner_password"
    owner_secret.write_text("owner-secret\n", encoding="utf-8")
    settings = module.Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        db_owner_password_file=owner_secret,
        **password_fields,
    )

    for role, username in module.DATABASE_ROLE_USERS.items():
        url = settings.database_url_for(role)
        assert url.username == username
        assert url.password == f"{role}-secret"
    assert settings.database_owner_url.username == "sms_owner"
    assert settings.database_owner_url.password == "owner-secret"


def test_runtime_credentials_are_read_from_configured_files(tmp_path: Path) -> None:
    module = load_settings_module()
    credential_names = (
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "jwt_secret",
        "ldap_bind_password",
    )
    kwargs: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "debug": True,
        "auth_mock": True,
        "vendor_mock": True,
    }
    for name in credential_names:
        secret_file = tmp_path / name
        secret_file.write_text(f"{name}-value\n", encoding="utf-8")
        kwargs[f"{name}_file"] = secret_file

    settings = module.Settings(**kwargs)

    for name in credential_names:
        assert settings.credential(name) == f"{name}-value"
    with pytest.raises(KeyError):
        settings.credential("unknown")
