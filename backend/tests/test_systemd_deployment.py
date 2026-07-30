from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "deploy" / "systemd" / "sms-platform.service"
AGENT_UNIT = ROOT / "deploy" / "systemd" / "vendor-control-agent.service"
SECURE_ACCESS_UNIT = (
    ROOT / "deploy" / "systemd" / "sms-platform-test-secure-access.service"
)
HOST_ENV = ROOT / "deploy" / "systemd" / "compose.env.example"
VENDOR_TEST_HOST_ENV = ROOT / "deploy" / "systemd" / "compose.vendor-test.env.example"
LIFECYCLE_ENV = ROOT / "deploy" / "systemd" / "lifecycle.env.example"
LIFECYCLE_CONFIG = ROOT / "deploy" / "lifecycle.server.example.json"
PARTITION_SERVICE = ROOT / "deploy" / "systemd" / "sms-partition-maintenance.service"
PARTITION_TIMER = ROOT / "deploy" / "systemd" / "sms-partition-maintenance.timer"
BACKUP_SERVICE = ROOT / "deploy" / "systemd" / "sms-backup.service"
BACKUP_TIMER = ROOT / "deploy" / "systemd" / "sms-backup.timer"
DRILL_SERVICE = ROOT / "deploy" / "systemd" / "sms-restore-drill.service"
DRILL_TIMER = ROOT / "deploy" / "systemd" / "sms-restore-drill.timer"
STATUS_SERVICE = ROOT / "deploy" / "systemd" / "sms-lifecycle-status.service"
STATUS_TIMER = ROOT / "deploy" / "systemd" / "sms-lifecycle-status.timer"
SECRET_NAMES = {
    "vendor_secret_name",
    "vendor_secret_key",
    "data_aes_key",
    "data_hmac_key",
    "jwt_secret",
    "ldap_bind_password",
    "metrics_scrape_token",
    "db_owner_password",
    "db_auth_password",
    "db_accept_password",
    "db_send_password",
    "db_callback_password",
    "db_export_password",
    "db_scheduler_password",
    "db_metrics_password",
    "redis_broker_password",
    "redis_auth_password",
    "redis_control_password",
}


def read_asset(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"systemd deployment asset is not implemented: {path.name}")
    return path.read_text(encoding="utf-8")


def test_systemd_deployment_assets_exist() -> None:
    assert UNIT.is_file(), "sms-platform systemd unit is not implemented"
    assert HOST_ENV.is_file(), "systemd host configuration example is not implemented"
    assert AGENT_UNIT.is_file(), "vendor control agent systemd unit is not implemented"
    assert SECURE_ACCESS_UNIT.is_file(), "test secure access unit is not implemented"
    assert VENDOR_TEST_HOST_ENV.is_file(), "vendor test host config is not implemented"
    for asset in (
        LIFECYCLE_ENV,
        LIFECYCLE_CONFIG,
        PARTITION_SERVICE,
        PARTITION_TIMER,
        BACKUP_SERVICE,
        BACKUP_TIMER,
        DRILL_SERVICE,
        DRILL_TIMER,
        STATUS_SERVICE,
        STATUS_TIMER,
    ):
        assert asset.is_file(), f"lifecycle asset is not implemented: {asset.name}"


def test_partition_timer_uses_controlled_owner_job_with_retry_and_hardening() -> None:
    service = read_asset(PARTITION_SERVICE)
    timer = read_asset(PARTITION_TIMER)

    for token in (
        "Requires=docker.service sms-platform.service",
        "EnvironmentFile=/etc/sms-platform/compose.env",
        "ExecStart=/usr/local/sbin/sms-compose partition-maintenance",
        "Restart=on-failure",
        "RestartSec=5min",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "RestrictAddressFamilies=AF_UNIX",
        "ReadWritePaths=/run/sms-platform /run/docker.sock",
    ):
        assert token in service
    for token in (
        "OnCalendar=*-*-* 01:15:00",
        "RandomizedDelaySec=15min",
        "Persistent=true",
        "WantedBy=timers.target",
    ):
        assert token in timer


def test_backup_restore_timers_are_automatic_persistent_and_randomized() -> None:
    backup_timer = read_asset(BACKUP_TIMER)
    drill_timer = read_asset(DRILL_TIMER)
    status_timer = read_asset(STATUS_TIMER)

    assert "OnCalendar=*-*-* 02:30:00" in backup_timer
    assert "OnCalendar=Sun *-*-* 04:00:00" in drill_timer
    assert "OnCalendar=hourly" in status_timer
    for timer in (backup_timer, drill_timer, status_timer):
        assert "Persistent=true" in timer
        assert "RandomizedDelaySec=" in timer
        assert "WantedBy=timers.target" in timer


def test_backup_restore_services_are_fail_closed_retried_and_hardened() -> None:
    expected_operations = {
        BACKUP_SERVICE: "backup",
        DRILL_SERVICE: "drill",
        STATUS_SERVICE: "status",
    }
    for path, operation in expected_operations.items():
        service = read_asset(path)
        for token in (
            "EnvironmentFile=/etc/sms-platform/lifecycle.env",
            f"lifecycle_manager.py {operation}",
            "Restart=on-failure",
            "RestartSec=5min",
            "StateDirectory=sms-platform/backups",
            "StateDirectoryMode=0700",
            "NoNewPrivileges=yes",
            "PrivateDevices=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "RestrictAddressFamilies=AF_UNIX",
            "CapabilityBoundingSet=",
            "StandardError=journal",
        ):
            assert token in service
        assert "Environment=BACKUP_PASSPHRASE" not in service
        assert "AF_INET" not in service


def test_lifecycle_examples_contain_only_paths_and_recovery_targets() -> None:
    lifecycle_env = {
        key: value
        for line in read_asset(LIFECYCLE_ENV).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for key, value in [line.split("=", 1)]
    }
    assert lifecycle_env == {
        "SMS_LIFECYCLE_CONFIG": "/etc/sms-platform/lifecycle.json",
        "BACKUP_PASSPHRASE_FILE": "/run/backup-secrets/sms-backup-passphrase",
    }
    assert all(value.startswith("/") for value in lifecycle_env.values())

    config = json.loads(read_asset(LIFECYCLE_CONFIG))
    assert config == {
        "schema_version": 1,
        "environment_file": "/etc/sms-platform/production.env",
        "output_root": "/var/lib/sms-platform/backups",
        "database": "sms",
        "retention_days": 35,
        "minimum_snapshots": 2,
        "max_backup_age_hours": 24,
        "max_restore_age_hours": 168,
        "max_restore_seconds": 1800,
    }
    serialized = json.dumps(config).casefold()
    for forbidden in ("secretkey", "jwt_secret", "ldap_bind_password", "phone"):
        assert forbidden not in serialized


def test_systemd_unit_coordinates_docker_and_compose_lifecycle() -> None:
    unit = read_asset(UNIT)

    for token in (
        "Requires=docker.service",
        "After=docker.service",
        "PartOf=docker.service",
        "Type=oneshot",
        "RemainAfterExit=yes",
        "EnvironmentFile=/etc/sms-platform/compose.env",
        "ExecStart=/usr/local/sbin/sms-compose up -d --remove-orphans",
        "ExecStop=/usr/local/sbin/sms-compose down --remove-orphans",
        "WantedBy=multi-user.target",
    ):
        assert token in unit


def test_systemd_unit_rate_limits_restart_on_start_failure() -> None:
    unit = read_asset(UNIT)

    assert "Restart=on-failure" in unit
    assert "RestartSec=" in unit
    assert "StartLimitIntervalSec=" in unit
    assert "StartLimitBurst=" in unit
    assert "Restart=always" not in unit


def test_systemd_environment_contains_only_paths_and_mode() -> None:
    host_env = read_asset(HOST_ENV)
    settings = {
        key: value
        for line in host_env.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for key, value in [line.split("=", 1)]
    }

    assert settings == {
        "SMS_PLATFORM_ROOT": "/opt/sms-platform",
        "SMS_SECRETS_MODE": "production",
        "SMS_RUNTIME_ROOT": "/run/sms-platform/secrets",
        "SMS_VENDOR_CREDENTIAL_ROOT": "/var/lib/sms-platform/vendor-test/credentials",
        "SMS_VENDOR_TEST_STATE_DIR": "/var/lib/sms-platform/vendor-test",
        "SMS_VENDOR_CONTROL_SOCKET_DIR": "/run/sms-platform/vendor-control",
    }


def test_vendor_test_environment_is_explicitly_development_and_mount_complete() -> None:
    settings = {
        key: value
        for line in read_asset(VENDOR_TEST_HOST_ENV).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for key, value in [line.split("=", 1)]
    }

    assert settings == {
        "SMS_PLATFORM_ROOT": "/opt/sms-platform",
        "SMS_SECRETS_MODE": "development",
        "SMS_RUNTIME_ROOT": "/run/sms-platform/secrets",
        "SMS_VENDOR_CREDENTIAL_ROOT": "/var/lib/sms-platform/vendor-test/credentials",
        "SMS_VENDOR_TEST_STATE_DIR": "/var/lib/sms-platform/vendor-test",
        "SMS_VENDOR_CONTROL_SOCKET_DIR": "/run/sms-platform/vendor-control",
    }


def test_systemd_assets_never_embed_credentials() -> None:
    combined = (
        f"{read_asset(UNIT)}\n{read_asset(AGENT_UNIT)}\n{read_asset(HOST_ENV)}\n"
        f"{read_asset(VENDOR_TEST_HOST_ENV)}"
    )

    for name in SECRET_NAMES:
        assert name not in combined
    for forbidden in ("DB_PASSWORD", "SecretKey", "JWT_SECRET", "LDAP_BIND_PASSWORD"):
        assert forbidden not in combined


def test_vendor_control_agent_unit_is_root_only_uds_and_hardened() -> None:
    unit = read_asset(AGENT_UNIT)

    for token in (
        "User=root",
        "Group=root",
        "EnvironmentFile=/etc/sms-platform/compose.env",
        "Before=sms-platform.service",
        "ExecStart=/opt/sms-platform/backend/.venv/bin/python",
        "deploy/scripts/vendor_control_agent.py",
        "--api-runtime-uid 10001",
        "--api-runtime-gid 10001",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectKernelTunables=yes",
        "ProtectControlGroups=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "RuntimeDirectory=sms-platform/vendor-control",
        "RuntimeDirectoryPreserve=yes",
        "StateDirectory=sms-platform/vendor-test",
        "StateDirectoryMode=0710",
        "UMask=0007",
    ):
        assert token in unit
    assert "ListenStream=" not in unit
    assert "0.0.0.0" not in unit
    assert "Environment=Secret" not in unit
    assert "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER" in unit
    for forbidden in ("sudo", "CAP_SYS_ADMIN", "AF_INET", "ExecStartPre=/bin/sh"):
        assert forbidden not in unit


def test_vendor_control_agent_allows_atomic_control_files_but_protects_code() -> None:
    unit = read_asset(AGENT_UNIT)

    # Atomic replacement needs write access to each controlled file's parent.
    assert "ReadWritePaths=/opt/sms-platform /etc/sms-platform" in unit
    # Every application surface used by the agent remains a more-specific
    # read-only mount; only the root .env is deliberately omitted.
    for path in (
        "/opt/sms-platform/.dockerignore",
        "/opt/sms-platform/.git",
        "/opt/sms-platform/.github",
        "/opt/sms-platform/.gitignore",
        "/opt/sms-platform/AGENTS.md",
        "/opt/sms-platform/CLAUDE.md",
        "/opt/sms-platform/HANDOVER.md",
        "/opt/sms-platform/PRD.md",
        "/opt/sms-platform/PROGRESS.md",
        "/opt/sms-platform/backend",
        "/opt/sms-platform/deploy",
        "/opt/sms-platform/docs",
        "/opt/sms-platform/frontend",
        "/opt/sms-platform/mise.toml",
        "/opt/sms-platform/openapi.yaml",
        "/opt/sms-platform/pyproject.toml",
        "/opt/sms-platform/schema.sql",
        "/opt/sms-platform/scripts",
        "/etc/sms-platform/compose.env",
        "/etc/sms-platform/production.env",
        "/etc/sms-platform/standby-sync.env",
        "/etc/sms-platform/test-host",
        "/etc/sms-platform/test-update-backup.json",
        "/etc/sms-platform/test-update-backup-key",
    ):
        assert f"ReadOnlyPaths=-{path}" in unit
    assert "ReadOnlyPaths=-/opt/sms-platform/.env" not in unit
    assert "ReadOnlyPaths=-/etc/sms-platform/test-environment" not in unit
    assert "StateDirectory=sms-platform/vendor-test" in unit
    assert "StateDirectory=sms-platform/vendor-test sms-platform/test-backups" not in unit
    writable = next(
        line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "/var/lib/sms-platform/test-backups" in writable
    assert "/opt/sms-platform/deploy/secrets" not in writable


def test_secure_access_unit_is_static_short_lived_and_non_privileged() -> None:
    unit = read_asset(SECURE_ACCESS_UNIT)

    for token in (
        "Requires=sms-platform.service",
        "After=network-online.target sms-platform.service",
        "Type=simple",
        "DynamicUser=yes",
        "ExecStart=/usr/bin/python3 "
        "/usr/local/libexec/sms-platform/test-secure-access/"
        "test_secure_access_runtime.py",
        "RuntimeDirectory=sms-platform-test-secure-access",
        "RuntimeDirectoryMode=0750",
        "RuntimeMaxSec=15min",
        "TimeoutStopSec=10s",
        "KillMode=control-group",
        "Restart=no",
        "SuccessExitStatus=SIGTERM 143",
        "Environment=HOME=/nonexistent",
        "Environment=XDG_CONFIG_HOME=/nonexistent",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "SystemCallArchitectures=native",
        "UMask=0027",
        "StandardOutput=null",
        "StandardError=journal",
    ):
        assert token in unit

    assert "[Install]" not in unit
    assert "WantedBy=" not in unit
    assert "Alias=" not in unit
    for forbidden in (
        "docker.sock",
        "/run/sms-platform/secrets",
        "/var/lib/sms-platform/vendor-test",
        "EnvironmentFile=",
        "SecretKey",
        "phone",
        "postgres",
        "redis",
        "0.0.0.0",
    ):
        assert forbidden.casefold() not in unit.casefold()


def test_systemd_runbook_serializes_release_mutations_with_unit_lifecycle() -> None:
    runbook = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    for token in (
        "systemd 不直接执行 release",
        "release activate/resume/rollback",
        "同一个 lifecycle flock",
        "不得并发执行 systemctl restart",
        "不得并发执行 systemctl stop",
        "RemainAfterExit=yes",
    ):
        assert token in runbook


def test_vendor_test_runbook_has_deterministic_mock_only_bootstrap() -> None:
    runbook = (ROOT / "docs/runbooks/controlled-real-vendor-test.md").read_text(
        encoding="utf-8"
    )

    for token in (
        "compose.vendor-test.env.example",
        "SMS_SECRETS_MODE=development",
        "VENDOR_MOCK=1",
        "vendor-test bootstrap",
        "systemctl restart sms-platform.service",
        "setup_required",
        "不得在 bootstrap 中输入正式 Key 或测试手机号",
    ):
        assert token in runbook


def test_vendor_reset_runbook_covers_runtime_revocation_and_replay_boundary() -> None:
    runbook = (ROOT / "docs/runbooks/controlled-real-vendor-test.md").read_text(
        encoding="utf-8"
    )
    deploy_readme = (ROOT / "deploy/README.md").read_text(encoding="utf-8")

    for token in (
        "credential store 与 runtime generations",
        "api、worker-realtime、worker-bulk",
        "runtime_revoked",
        "journal `running`",
        "同一 lifecycle lock",
        "不得回切旧 runtime generation",
        "PostgreSQL、Docker volume 和非厂商 secret",
    ):
        assert token in runbook
    for token in (
        "vendor-test reset-runtime",
        "release 共用同一个 lifecycle flock",
        "固定 revocation tombstone",
        "不扩大 systemd capability",
    ):
        assert token in deploy_readme
