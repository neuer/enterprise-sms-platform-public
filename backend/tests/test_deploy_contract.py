from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_compose() -> dict[str, Any]:
    document = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def test_container_build_files_use_locked_runtime_bases() -> None:
    backend = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    postgres = (ROOT / "deploy/postgres.Dockerfile").read_text(encoding="utf-8")
    redis = (ROOT / "deploy/redis.Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM ghcr.io/astral-sh/uv:0.7.12@sha256:"
        "4faec156e35a5f345d57804d8858c6ba1cf6352ce5f4bffc11b7fdebdef46a38" in backend
    )
    assert (
        "FROM python:3.12-alpine@sha256:"
        "6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df" in backend
    )
    assert "uv sync --frozen --no-dev" in backend
    assert (
        "FROM node:24-alpine@sha256:"
        "a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd" in frontend
    )
    assert "npm ci" in frontend
    assert "USER 101:101" in frontend
    assert (
        "FROM nginx:stable-alpine@sha256:"
        "0d3b80406a13a767339fbe2f41406d6c7da727ab89cf8fae399e81f780f814d1" in frontend
    )
    assert "apk upgrade" not in frontend
    assert "apk add" not in backend
    assert (
        "FROM postgres:16-alpine@sha256:"
        "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777" in postgres
    )
    assert "apk add --no-cache su-exec=0.3-r0" in postgres
    assert "rm /usr/local/bin/gosu" in postgres
    assert "ln -s /sbin/su-exec /usr/local/bin/gosu" in postgres
    assert "AS prepared" in postgres
    assert "FROM scratch" in postgres
    assert "COPY --from=prepared / /" in postgres
    for metadata in (
        "ENV PGDATA=/var/lib/postgresql/data",
        "EXPOSE 5432",
        'VOLUME ["/var/lib/postgresql/data"]',
        "USER 70:70",
        'ENTRYPOINT ["docker-entrypoint.sh"]',
        'CMD ["postgres"]',
        "STOPSIGNAL SIGINT",
    ):
        assert metadata in postgres
    assert (
        "FROM redis:7-alpine@sha256:"
        "6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99" in redis
    )
    assert "USER 999:1000" in redis


def test_compose_builds_and_names_the_same_four_release_images() -> None:
    compose = load_compose()
    services = compose["services"]
    backend = compose["x-backend"]

    assert backend["image"] == "${SMS_API_IMAGE:-sms-platform-api:local}"
    assert backend["build"]["dockerfile"] == "backend/Dockerfile"

    expected = {
        "postgres": (
            "${SMS_POSTGRES_IMAGE:-sms-platform-postgres:local}",
            "deploy/postgres.Dockerfile",
        ),
        "redis": (
            "${SMS_REDIS_IMAGE:-sms-platform-redis:local}",
            "deploy/redis.Dockerfile",
        ),
        "web": ("${SMS_WEB_IMAGE:-sms-platform-web:local}", "frontend/Dockerfile"),
    }
    for service_name, (image, dockerfile) in expected.items():
        service = services[service_name]
        assert service["image"] == image
        assert service["build"] == {"context": "..", "dockerfile": dockerfile}


def test_api_uses_two_workers_in_image_and_compose_contracts() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    api_command = load_compose()["services"]["api"]["command"].split()

    assert '"--workers", "2"' in dockerfile
    assert api_command.count("--workers") == 1
    worker_index = api_command.index("--workers")
    assert api_command[worker_index + 1] == "2"


def test_web_healthcheck_uses_wget_available_in_the_final_nginx_image() -> None:
    web = load_compose()["services"]["web"]

    assert web["healthcheck"] == {
        "test": [
            "CMD-SHELL",
            "wget -q --spider http://127.0.0.1:8080/",
        ],
        "interval": "10s",
        "timeout": "5s",
        "retries": 12,
        "start_period": "10s",
    }


def test_frontend_requires_node_24() -> None:
    package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))

    assert package["engines"]["node"] == ">=24 <25"
    assert lock["packages"][""]["engines"]["node"] == ">=24 <25"


def test_root_build_context_excludes_credentials_and_local_artifacts() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env*" in patterns
    assert "deploy/secrets/" in patterns
    assert "**/node_modules/" in patterns
    assert "**/.venv/" in patterns
    assert ".git/" in patterns


def test_runtime_services_never_mount_owner_password() -> None:
    compose = load_compose()
    services = compose["services"]
    assert isinstance(services, dict)

    assert services["postgres"]["secrets"] == [
        {"source": "postgres_db_owner_password", "target": "db_owner_password"}
    ]
    provision = services["db-role-provision"]
    assert provision["depends_on"] == {
        "postgres": {"condition": "service_healthy"}
    }
    assert {item["target"] for item in provision["secrets"]} == {
        "db_owner_password",
        "db_auth_password",
        "db_accept_password",
        "db_send_password",
        "db_callback_password",
        "db_export_password",
        "db_scheduler_password",
        "db_metrics_password",
    }
    migrate = services["migrate"]
    assert migrate["environment"]["DB_OWNER_PASSWORD_FILE"] == (
        "/run/secrets/db_owner_password"
    )
    assert migrate["secrets"] == [
        {"source": "db_owner_password", "target": "db_owner_password"}
    ]

    expected_roles = {
        "api": "accept",
        "worker-realtime": "send",
        "worker-bulk": "send",
        "worker-callback": "callback",
        "outbox-dispatcher": "scheduler",
        "beat": "scheduler",
    }
    for name, role in expected_roles.items():
        service = services[name]
        assert service["environment"]["DB_RUNTIME_ROLE"] == role
        targets = {item["target"] for item in service["secrets"]}
        assert f"db_{role}_password" in targets
        assert "db_owner_password" not in targets

    runtime = "${SMS_RUNTIME_SECRETS_DIR:-/run/sms-platform/secrets/current}"
    secrets = compose["secrets"]
    assert secrets["db_owner_password"]["file"] == f"{runtime}/migrate/db_owner_password"
    assert secrets["postgres_db_owner_password"]["file"] == (
        f"{runtime}/postgres/db_owner_password"
    )
    for role in ("auth", "accept", "send", "callback", "export", "scheduler", "metrics"):
        assert secrets[f"db_{role}_password"]["file"] == (
            f"{runtime}/backend/db_{role}_password"
        )
        assert secrets[f"postgres_db_{role}_password"]["file"] == (
            f"{runtime}/postgres/db_{role}_password"
        )


def test_database_role_provision_repairs_attributes_and_search_path_boundary() -> None:
    script = (ROOT / "deploy/provision-db-roles.sh").read_text(encoding="utf-8")

    safe_attributes = (
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT"
    )
    for role in ("auth", "accept", "send", "callback", "export", "scheduler", "metrics"):
        assert f"'ALTER ROLE sms_{role} {safe_attributes} PASSWORD %L'" in script
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC" in script
    assert "PASSWORD %L" in script
    assert "ALTER ROLE sms_app LOGIN" not in script


def test_backend_services_mount_only_role_required_secrets() -> None:
    compose = load_compose()
    services = compose["services"]

    def targets(service_name: str) -> set[str]:
        return {item["target"] for item in services[service_name]["secrets"]}

    assert "secrets" not in compose["x-backend"]
    assert targets("api") == {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "jwt_secret",
        "ldap_bind_password",
        "metrics_scrape_token",
        "db_auth_password",
        "db_accept_password",
        "db_callback_password",
        "db_export_password",
        "db_metrics_password",
        "redis_auth_password",
        "redis_control_password",
    }
    assert targets("worker-realtime") == {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "db_send_password",
        "db_callback_password",
        "redis_broker_password",
        "redis_control_password",
    }
    assert targets("worker-bulk") == {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "db_send_password",
        "db_export_password",
        "redis_broker_password",
        "redis_control_password",
    }
    assert targets("worker-callback") == {
        "data_aes_key",
        "data_hmac_key",
        "db_callback_password",
        "redis_broker_password",
        "redis_control_password",
    }
    assert targets("beat") == {
        "db_scheduler_password",
        "redis_broker_password",
        "redis_control_password",
    }
    assert targets("outbox-dispatcher") == {
        "db_scheduler_password",
        "redis_broker_password",
    }
    assert "redis_broker_password" not in targets("api")
    assert "redis_auth_password" not in targets("worker-callback")


def test_compose_uses_locking_beat_entrypoint_and_fixed_queues() -> None:
    compose = load_compose()
    services = compose["services"]
    assert (
        services["worker-realtime"]["command"]
        == "celery -A app.tasks worker -Q realtime -c 2 -l info"
    )
    assert services["worker-bulk"]["command"] == "celery -A app.tasks worker -Q bulk -c 2 -l info"
    assert (
        services["worker-callback"]["command"]
        == "celery -A app.tasks worker -Q callback -c 2 -l info"
    )
    assert services["beat"]["command"] == "python -m app.tasks.beat"
    assert services["outbox-dispatcher"]["command"] == "python -m app.outbox_dispatcher"


def test_host_ports_are_overridable_without_changing_container_contract() -> None:
    compose = load_compose()
    services = compose["services"]

    assert services["api"]["ports"] == ["127.0.0.1:${API_PORT:-8000}:8000"]
    assert services["web"]["ports"] == ["${WEB_BIND_IP:-127.0.0.1}:${WEB_PORT:-18080}:8080"]
    assert services["mock-vendor"]["ports"] == ["127.0.0.1:${MOCK_VENDOR_PORT:-9028}:9028"]


def test_production_docs_use_reserved_public_ports() -> None:
    env_example = (ROOT / "deploy/.env.example").read_text(encoding="utf-8")
    runbook = (ROOT / "deploy/README.md").read_text(encoding="utf-8")

    assert "WEB_BASE_URL=https://sms.example.com:18443" in env_example
    assert "明文 HTTP 上游默认绑定回环" in runbook
    assert "`18443`" in runbook
    assert "80/443/8080/8443/8000/9028" in runbook


def test_nginx_serves_spa_and_proxies_api() -> None:
    config = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")

    assert "server_tokens off" in config
    assert "try_files $uri $uri/ /index.html" in config
    assert "livez|readyz|healthz" in config
    assert "location /api/" in config
    assert "proxy_pass http://api:8000;" in config
    assert config.count("proxy_pass http://api:8000;") == 2
    assert "proxy_pass http://api:8000/;" not in config


def test_nginx_enforces_browser_policies_without_internal_hsts() -> None:
    config = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    headers = (ROOT / "deploy/nginx-security-headers.conf").read_text(encoding="utf-8")

    assert "add_header Content-Security-Policy" in headers
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "script-src-attr 'none'",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in headers
    assert "script-src 'self' 'unsafe-inline'" not in headers
    assert "style-src 'self' 'unsafe-inline'" not in headers
    assert (
        'add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), '
        'payment=(), usb=()" always;' in headers
    )
    assert config.count("include /etc/nginx/browser-security-headers.conf;") == 3
    assert "Strict-Transport-Security" not in config
    assert "Strict-Transport-Security" not in headers


def test_api_trusts_only_fixed_nginx_proxy_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
    compose = load_compose()
    command = compose["services"]["api"]["command"]
    assert "--proxy-headers" in command
    assert "--forwarded-allow-ips 172.31.250.3" in command
    assert "TRUSTED_PROXY_IPS" not in command
    assert "*" not in command
    raw_compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "TRUSTED_PROXY_IPS" not in raw_compose
    assert compose["services"]["web"]["networks"]["ingress"]["ipv4_address"] == (
        "${SMS_WEB_INGRESS_IPV4:-172.31.250.3}"
    )
    config = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "proxy_set_header X-Real-IP $remote_addr;" in config
    assert "proxy_set_header X-Forwarded-Proto $forwarded_proto;" in config
    assert "map \"$sms_trusted_proxy|$http_x_forwarded_proto\" $forwarded_proto" in config
    assert "$http_x_forwarded_for" not in config
    assert "include /etc/nginx/trusted-proxies.conf;" in config
    assert "$proxy_add_x_forwarded_for" not in config
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    assert (
        "COPY deploy/trusted-proxies.conf /etc/nginx/trusted-proxies.conf" in dockerfile
    )
    trusted = (ROOT / "deploy/trusted-proxies.conf").read_text(encoding="utf-8")
    assert "geo $realip_remote_addr $sms_trusted_proxy" in trusted
    assert "default 0;" in trusted
    web_volumes = compose["services"]["web"]["volumes"]
    assert any(
        str(volume).endswith(
            "/etc/nginx/trusted-proxies.conf:ro"
        )
        for volume in web_volumes
    )
    assert any(
        str(volume).startswith(
            "${SMS_TRUSTED_PROXY_CONF:-/usr/local/share/sms-platform/trusted-proxies.conf}"
        )
        for volume in web_volumes
    )
    assert "deploy/trusted-proxies.conf" not in " ".join(map(str, web_volumes))
    assert compose["services"]["web"]["group_add"] == ["${SMS_WEB_HOST_GID:-1000}"]
    example = (ROOT / "deploy/.env.example").read_text(encoding="utf-8")
    assert "SMS_WEB_HOST_GID=1000" in example


def test_phone_query_strings_are_excluded_from_access_logs() -> None:
    config = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    backend = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    compose = load_compose()

    assert "log_format pii_safe" in config
    assert "access_log /var/log/nginx/access.log pii_safe" in config
    log_format = config.split("log_format pii_safe", 1)[1].split(";", 1)[0]
    assert "$time_local" in log_format
    assert "$uri" in log_format
    assert "$request_uri" not in log_format and "$request" not in log_format
    assert '"--no-access-log"' in backend
    assert "--no-access-log" in compose["services"]["api"]["command"]


def test_postgres_error_logs_keep_errors_but_suppress_pii_statements_and_detail() -> None:
    postgres = load_compose()["services"]["postgres"]
    command = postgres["command"]
    assert command == [
        "postgres",
        "-c",
        "log_error_verbosity=terse",
        "-c",
        "log_min_error_statement=panic",
        "-c",
        "log_parameter_max_length_on_error=0",
    ]


def test_export_ciphertext_volume_is_shared_only_by_api_and_bulk_worker() -> None:
    services = load_compose()["services"]
    assert "exportdata:/var/lib/sms/exports" in services["api"]["volumes"]
    assert "exportdata:/var/lib/sms/exports" in services["worker-bulk"]["volumes"]
    for name in ("worker-realtime", "worker-callback", "outbox-dispatcher", "beat"):
        assert "exportdata:/var/lib/sms/exports" not in services[name].get("volumes", [])

    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "mkdir -p /var/lib/sms/imports /var/lib/sms/exports" in dockerfile
    assert "chown -R sms:sms /var/lib/sms" in dockerfile


def test_lifecycle_partition_maintenance_stays_in_owner_migrate_boundary() -> None:
    services = load_compose()["services"]
    migrate = services["migrate"]
    assert migrate["command"] == [
        "sh",
        "-ec",
        "alembic upgrade head && python -m scripts_support.maintain_partitions",
    ]
    assert migrate["environment"]["DB_OWNER_PASSWORD_FILE"] == (
        "/run/secrets/db_owner_password"
    )
    assert "importdata:/var/lib/sms/imports" in services["worker-bulk"]["volumes"]

    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "COPY backend/scripts_support ./scripts_support" in dockerfile
