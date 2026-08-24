from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


def contract() -> ModuleType:
    try:
        return importlib.import_module("test_secure_access_contract")
    except ModuleNotFoundError:
        pytest.fail("test secure access contract is not implemented")


def test_contract_pins_every_host_runtime_boundary() -> None:
    module = contract()

    assert module.CLOUDFLARED_VERSION == "2026.7.2"
    assert (
        module.CLOUDFLARED_SHA256
        == "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd"
    )
    assert Path("/usr/local/libexec/sms-platform/cloudflared") == module.CLOUDFLARED_PATH
    assert module.SERVICE_NAME == "sms-platform-test-secure-access.service"
    assert module.PERSISTENT_SERVICE_NAME == "sms-platform-cloudflare-tunnel.service"
    assert Path("/etc/sms-platform/test-host") == module.TEST_HOST_MARKER_PATH
    assert module.ORIGIN == "http://127.0.0.1:18080"
    assert Path("/run/sms-platform-test-secure-access/status.json") == module.STATUS_PATH
    assert module.MAX_LIFETIME_SECONDS == 900
    assert Path("/usr/local/libexec/sms-platform/test-secure-access") == module.HOST_ASSET_ROOT
    assert module.HOST_MANIFEST_PATH == module.HOST_ASSET_ROOT / "manifest.json"
    assert set(module.HOST_ASSET_NAMES) == {
        "install_test_secure_access.py",
        "test_secure_access_contract.py",
        "test_secure_access_runtime.py",
        "test_secure_access_manager.py",
        "cloudflare_tunnel_manager.py",
        "vendor_test_files.py",
        "check_test_update_migration.py",
        "run_with_lifecycle_lock.py",
        "public_baseline_activation.py",
        "public_baseline_manager.py",
        "public_cutover_bootstrap.py",
        "test_update_apply.py",
        "test_update_backup.py",
        "test_update_contract.py",
        "test_update_manager.py",
        "test_update_promote.py",
        "test_update_store.py",
        "test_update_verify.py",
        "protected_path_policy.py",
        "check_public_readiness.py",
        "export_public_snapshot.py",
        "verify_public_snapshot_cutover.py",
        "verify_web_transport.py",
        "sms-compose-bootstrap",
        "sms-platform-test-secure-access.service",
        "sms-platform-cloudflare-tunnel.service",
        "cloudflared",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://safe-name.trycloudflare.com",
        "https://safe-name.trycloudflare.com/",
        "https://safe-name.trycloudflare.com/login",
        "https://safe-name.trycloudflare.com?token=x",
        "https://safe-name.trycloudflare.com#fragment",
        "https://user@safe-name.trycloudflare.com",
        "https://safe-name.trycloudflare.com:443",
        "https://safe-name.trycloudflare.com.example.test",
        "https://SAFE-NAME.trycloudflare.com",
        "https://-unsafe.trycloudflare.com",
        "https://unsafe-.trycloudflare.com",
        "https://two.labels.trycloudflare.com",
    ],
)
def test_contract_rejects_non_exact_quick_tunnel_urls(url: str) -> None:
    module = contract()

    with pytest.raises(module.SecureAccessContractError, match="URL"):
        module.parse_quick_tunnel_url(url)


def test_contract_accepts_only_one_lowercase_quick_tunnel_label() -> None:
    module = contract()
    url = "https://sample-label.trycloudflare.com"

    assert module.parse_quick_tunnel_url(url) == url


def test_ready_state_round_trips_with_exact_fields_and_timezone() -> None:
    module = contract()
    started_at = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
    expires_at = started_at + timedelta(seconds=900)
    url = "https://safe-name.trycloudflare.com"

    raw = module.serialize_ready_state(
        url=url,
        started_at=started_at,
        expires_at=expires_at,
    )
    state = module.parse_ready_state(raw)

    assert json.loads(raw) == {
        "schema_version": 1,
        "status": "ready",
        "url": url,
        "started_at": "2026-07-19T09:00:00+00:00",
        "expires_at": "2026-07-19T09:15:00+00:00",
    }
    assert state.url == url
    assert state.started_at == started_at
    assert state.expires_at == expires_at


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"schema_version":1,"status":"ready","url":"https://safe-name.trycloudflare.com",'
        '"started_at":"2026-07-19T09:00:00","expires_at":"2026-07-19T09:15:00+00:00"}',
        '{"schema_version":1,"status":"ready","url":"https://safe-name.trycloudflare.com",'
        '"started_at":"2026-07-19T09:00:00+00:00",'
        '"expires_at":"2026-07-19T09:15:01+00:00"}',
        '{"schema_version":1,"status":"ready","url":"https://safe-name.trycloudflare.com",'
        '"started_at":"2026-07-19T09:00:00+00:00",'
        '"expires_at":"2026-07-19T09:15:00+00:00","extra":true}',
        '{"schema_version":1,"schema_version":1,"status":"ready",'
        '"url":"https://safe-name.trycloudflare.com",'
        '"started_at":"2026-07-19T09:00:00+00:00",'
        '"expires_at":"2026-07-19T09:15:00+00:00"}',
    ],
)
def test_ready_state_rejects_unknown_duplicate_or_invalid_values(raw: str) -> None:
    module = contract()

    with pytest.raises(module.SecureAccessContractError, match="state"):
        module.parse_ready_state(raw)


def test_host_manifest_round_trips_only_exact_asset_digests() -> None:
    module = contract()
    digests = {name: f"{index:064x}" for index, name in enumerate(module.HOST_ASSET_NAMES, start=1)}

    raw = module.serialize_host_manifest(digests, source_commit="a" * 40)

    parsed = module.parse_host_manifest(raw)
    assert parsed.source_commit == "a" * 40
    assert parsed.files == digests


@pytest.mark.parametrize(
    "mutate",
    ["missing", "unknown", "bad-digest", "duplicate"],
)
def test_host_manifest_rejects_drifted_shape_or_digest(
    mutate: str,
) -> None:
    module = contract()
    digests = {name: f"{index:064x}" for index, name in enumerate(module.HOST_ASSET_NAMES, start=1)}
    document = {
        "schema_version": 1,
        "source_commit": "a" * 40,
        "files": dict(digests),
    }
    if mutate == "missing":
        document["files"].pop(next(iter(digests)))
        raw = json.dumps(document)
    elif mutate == "unknown":
        document["files"]["unexpected"] = "0" * 64
        raw = json.dumps(document)
    elif mutate == "bad-digest":
        document["files"][next(iter(digests))] = "not-a-digest"
        raw = json.dumps(document)
    else:
        name = next(iter(digests))
        raw = (
            '{"schema_version":1,"source_commit":"' + "a" * 40 + '","files":{'
            f'"{name}":"{digests[name]}","{name}":"{digests[name]}"'
            "}}"
        )

    with pytest.raises(module.SecureAccessContractError, match="manifest"):
        module.parse_host_manifest(raw)


def test_test_host_marker_round_trips_exact_fixed_document() -> None:
    module = contract()

    raw = module.serialize_test_host_marker()

    assert module.parse_test_host_marker(raw) is None
    assert json.loads(raw) == {
        "mode": "development-test-host",
        "purpose": "temporary-https-and-fast-update",
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"schema_version":1,"mode":"development-vendor-live",'
        '"purpose":"temporary-https-and-fast-update"}',
        '{"schema_version":1,"mode":"development-test-host",'
        '"purpose":"temporary-https-and-fast-update","purpose":"duplicate"}',
    ],
)
def test_test_host_marker_rejects_non_exact_documents(raw: str) -> None:
    module = contract()

    with pytest.raises(module.SecureAccessContractError, match="test host marker"):
        module.parse_test_host_marker(raw)
