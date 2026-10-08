from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load(
        (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    )
    return cast(dict[str, Any], loaded)


def test_vendor_test_state_is_read_only_and_only_visible_to_vendor_callers() -> None:
    services = _compose()["services"]
    mount = (
        "${SMS_VENDOR_TEST_STATE_DIR:-./vendor-test-empty}:"
        "/run/vendor-test:ro"
    )

    for service in ("api", "worker-realtime", "worker-report", "worker-bulk"):
        assert mount in services[service].get("volumes", [])
    for service in (
        "postgres",
        "redis",
        "migrate",
        "worker-callback",
        "outbox-dispatcher",
        "beat",
        "mock-vendor",
        "web",
    ):
        assert mount not in services[service].get("volumes", [])


def test_auth_policy_mount_is_read_only_and_only_visible_to_api() -> None:
    mount = "${SMS_AUTH_POLICY_DIR:-./auth-policy}:/run/auth-policy:ro"
    for name, service in _compose()["services"].items():
        assert (mount in service.get("volumes", [])) == (name == "api")
    assert (ROOT / "deploy/auth-policy/.gitkeep").is_file()


def test_vendor_control_socket_directory_is_read_only_and_least_privilege() -> None:
    services = _compose()["services"]
    mount = (
        "${SMS_VENDOR_CONTROL_SOCKET_DIR:-./vendor-control-empty}:"
        "/run/vendor-control:ro"
    )

    for service in ("api", "worker-realtime"):
        assert mount in services[service].get("volumes", [])
    for service in (
        "postgres",
        "redis",
        "migrate",
        "worker-report",
        "worker-bulk",
        "worker-callback",
        "outbox-dispatcher",
        "beat",
        "mock-vendor",
        "web",
    ):
        assert mount not in services[service].get("volumes", [])

    for service in services.values():
        for volume in service.get("volumes", []):
            assert "/run/sms-platform/secrets" not in volume
            assert "docker.sock" not in volume

    assert services["api"]["user"] == "10001:10001"
    assert services["worker-realtime"]["user"] == "10001:10002"
    assert services["worker-realtime"]["group_add"] == ["10001"]
    assert services["worker-realtime"]["tmpfs"] == [
        "/tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10002,mode=0700"
    ]


def test_live_profile_does_not_auto_start_mock_and_default_state_dir_has_no_allowlist() -> None:
    compose = _compose()

    assert compose["services"]["mock-vendor"]["profiles"] == ["dev"]
    default_allowlist = ROOT / "deploy/vendor-test-empty/allowlist.json"
    assert not default_allowlist.exists()
    assert (ROOT / "deploy/vendor-test-empty/.gitkeep").is_file()


def test_outbox_dispatcher_retries_broker_without_startup_coupling() -> None:
    service = _compose()["services"]["outbox-dispatcher"]

    assert service["command"] == "python -m app.outbox_dispatcher"
    assert service["depends_on"] == {
        "migrate": {"condition": "service_completed_successfully"}
    }
    assert service["secrets"] == [
        {"source": "db_scheduler_password", "target": "db_scheduler_password"},
        {"source": "redis_broker_dispatcher_client_password", "target": "redis_broker_password"},
    ]
    assert "redis" not in service["depends_on"]
    assert "db_owner_password" not in service.get("secrets", [])


def test_backend_services_declare_database_pool_component() -> None:
    services = _compose()["services"]
    expected = {
        "api": "api",
        "worker-realtime": "worker",
        "worker-report": "worker",
        "worker-bulk": "worker",
        "worker-callback": "worker",
        "beat": "beat",
        "outbox-dispatcher": "background",
    }

    assert {
        name: services[name]["environment"]["SMS_COMPONENT"]
        for name in expected
    } == expected


def test_every_runtime_service_has_minimal_read_only_resource_boundary() -> None:
    services = _compose()["services"]
    runtime_services = {
        "postgres",
        "redis",
        "migrate",
        "api",
        "worker-realtime",
        "worker-report",
        "worker-bulk",
        "worker-callback",
        "beat",
        "outbox-dispatcher",
        "mock-vendor",
        "web",
    }

    for name in runtime_services:
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["user"] not in {"0", "0:0", "root"}
        assert int(service["pids_limit"]) > 0
        assert service["mem_limit"]
        assert float(service["cpus"]) > 0
        assert service.get("privileged") is not True
        assert all(
            "noexec" in mount and "nosuid" in mount and "nodev" in mount
            for mount in service["tmpfs"]
        )




def test_api_healthcheck_uses_bounded_dependency_readiness() -> None:
    healthcheck = _compose()["services"]["api"]["healthcheck"]

    assert healthcheck["test"] == [
        "CMD",
        "python",
        "-m",
        "app.healthcheck",
        "ready",
    ]
    assert healthcheck["timeout"] == "3s"
    assert healthcheck["start_period"] == "30s"


def test_broker_secrets_are_per_service_and_never_mount_derivation_seed() -> None:
    compose = _compose()
    roles = {"worker-realtime": "realtime", "worker-report": "report", "worker-bulk": "bulk",
             "worker-callback": "callback", "beat": "beat", "outbox-dispatcher": "dispatcher"}
    sources = set()
    for name, role in roles.items():
        service = compose["services"][name]
        assert service["environment"]["REDIS_BROKER_ROLE"] == role
        credentials = [item["source"] for item in service["secrets"]
                       if item["target"] == "redis_broker_password"]
        assert credentials == [f"redis_broker_{role}_client_password"]
        path = compose["secrets"][credentials[0]]["file"]
        assert path.endswith(f"/backend/redis_broker_{role}_password")
        sources.update(credentials)
        assert all(item["target"] != "redis_broker_users.acl" for item in service["secrets"])
        if name.startswith("worker-"):
            for flag in ("--without-gossip", "--without-mingle", "--without-heartbeat"):
                assert flag in service["command"]
    assert len(sources) == len(roles)
    for name, service in compose["services"].items():
        for item in service.get("secrets", []):
            if isinstance(item, dict) and item["source"] in {
                "redis_broker_server_password", "redis_broker_users_acl"
            }:
                assert name == "redis"
