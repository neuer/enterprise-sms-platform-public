from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))
sys.path.insert(0, str(ROOT / "scripts"))


def manager_module() -> ModuleType:
    try:
        return importlib.import_module("cloudflare_tunnel_manager")
    except ModuleNotFoundError:
        pytest.fail("cloudflare tunnel manager is not implemented")


class FakeRunner:
    def __init__(self) -> None:
        self.states = {
            "sms-platform.service": "active",
            "sms-platform-cloudflare-tunnel.service": "inactive",
        }
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[:2] == ("systemctl", "is-active"):
            state = self.states.get(argv[2], "unknown")
            result = subprocess.CompletedProcess(
                argv,
                0 if state == "active" else 3,
                f"{state}\n",
                "",
            )
        elif argv[:3] == ("systemctl", "enable", "--now"):
            self.states[argv[3]] = "active"
            result = subprocess.CompletedProcess(argv, 0, "", "")
        elif argv[:3] == ("systemctl", "disable", "--now"):
            self.states[argv[3]] = "inactive"
            result = subprocess.CompletedProcess(argv, 0, "", "")
        else:
            result = subprocess.CompletedProcess(argv, 0, "", "")
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result


class FakeOriginProbe:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def check(self) -> bool:
        return self.available


def fixture(tmp_path: Path):
    module = manager_module()
    contract = importlib.import_module("test_secure_access_contract")
    root = tmp_path / "host-assets"
    root.mkdir()
    binary = tmp_path / "bin/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"fixed-cloudflared")
    binary.chmod(0o755)
    for name in contract.HOST_ASSET_NAMES:
        if name == "cloudflared":
            continue
        path = root / name
        if name == "sms-platform-cloudflare-tunnel.service":
            payload = "[Service]\nExecStart=fixed --token-file %d/tunnel-token\n"
        elif name == "sms-compose-bootstrap":
            payload = "#!/usr/bin/env bash\nexit 0\n"
        else:
            payload = f"# fixed {name}\n"
        path.write_text(payload, encoding="utf-8")
        path.chmod(0o755 if name == "sms-compose-bootstrap" else 0o644)
    installed_unit = tmp_path / "etc/systemd/system/sms-platform-cloudflare-tunnel.service"
    installed_unit.parent.mkdir(parents=True)
    config_path = tmp_path / "etc/sms-platform/cloudflare-tunnel.json"
    config_path.parent.mkdir(parents=True)
    token_path = config_path.parent / "cloudflare-tunnel-token"
    paths = {
        name: binary if name == "cloudflared" else root / name for name in contract.HOST_ASSET_NAMES
    }
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    manifest = root / "manifest.json"
    manifest.write_text(
        contract.serialize_host_manifest(digests, source_commit="a" * 40),
        encoding="utf-8",
    )
    manifest.chmod(0o644)
    runner = FakeRunner()
    manager = module.CloudflareTunnelManager(
        root=root,
        binary_path=binary,
        manifest_path=manifest,
        config_path=config_path,
        token_path=token_path,
        installed_unit=installed_unit,
        expected_uid=os.geteuid(),
        expected_sha256=digests["cloudflared"],
        runner=runner,
        origin_probe=FakeOriginProbe(),
        token_reader=lambda: "eyJ" + "a" * 80,
        sleeper=lambda _seconds: None,
    )
    return module, manager, runner, root, binary, installed_unit, config_path, token_path


@pytest.mark.parametrize(
    "hostname",
    (
        "SMS.example.invalid",
        "sms",
        "sms.example.invalid.",
        "https://sms.example.invalid",
        "-sms.example.invalid",
        "sms_example.invalid",
    ),
)
def test_hostname_contract_rejects_non_canonical_fqdn(hostname: str) -> None:
    module = manager_module()

    with pytest.raises(module.CloudflareTunnelManagerError, match="hostname"):
        module.parse_hostname(hostname)


@pytest.mark.parametrize(
    "argv",
    (
        [],
        ["configure"],
        ["configure", "--hostname", "https://evil.test"],
        ["install-token", "secret-on-argv"],
        ["start", "--token", "secret-on-argv"],
        ["shell"],
    ),
)
def test_cli_contract_rejects_arbitrary_or_secret_arguments(argv: list[str]) -> None:
    module = manager_module()

    with pytest.raises(module.CloudflareTunnelManagerError, match="invocation|hostname"):
        module.parse_manager_action(argv, euid=0, mode="development")


def test_persistent_tunnel_lifecycle_keeps_token_out_of_process_arguments(
    tmp_path: Path,
) -> None:
    (
        _,
        manager,
        runner,
        _,
        _,
        installed_unit,
        config_path,
        token_path,
    ) = fixture(tmp_path)

    installed = manager.install()
    configured = manager.configure("sms.example.invalid")
    token_installed = manager.install_token()
    started = manager.start()

    from verify_web_transport import TransportEvidence

    manager.transport_probe = lambda **_kwargs: TransportEvidence(
        redirect_status=301,
        tls_version="TLSv1.3",
        certificate_days_remaining=89,
        hsts_max_age=31_536_000,
    )
    verified = manager.verify()
    stopped = manager.stop()

    assert installed.status == "inactive"
    assert configured.hostname == "sms.example.invalid"
    assert token_installed.token_configured is True
    assert started.status == "active"
    assert verified.status == "verified"
    assert verified.tls_version == "TLSv1.3"
    assert verified.certificate_days_remaining == 89
    assert stopped.status == "inactive"
    assert installed_unit.stat().st_mode & 0o777 == 0o644
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "hostname": "sms.example.invalid",
        "origin": "http://127.0.0.1:18080",
    }
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert token_path.read_text(encoding="ascii").startswith("eyJ")
    flattened = " ".join(" ".join(call) for call in runner.calls)
    assert token_path.read_text(encoding="ascii").strip() not in flattened
    assert (
        "systemctl",
        "enable",
        "--now",
        "sms-platform-cloudflare-tunnel.service",
    ) in runner.calls
    assert (
        "systemctl",
        "disable",
        "--now",
        "sms-platform-cloudflare-tunnel.service",
    ) in runner.calls


def test_start_fails_closed_when_loopback_origin_is_unavailable(tmp_path: Path) -> None:
    module, manager, runner, *_ = fixture(tmp_path)
    manager.install()
    manager.configure("sms.example.invalid")
    manager.install_token()
    manager.origin_probe = FakeOriginProbe(False)

    with pytest.raises(module.CloudflareTunnelManagerError, match="origin"):
        manager.start()

    assert runner.states["sms-platform-cloudflare-tunnel.service"] == "inactive"


def test_manager_rejects_manifest_bound_asset_drift(tmp_path: Path) -> None:
    module, manager, runner, root, *_ = fixture(tmp_path)
    (root / "sms-platform-cloudflare-tunnel.service").write_text(
        "[Service]\nExecStart=drifted\n",
        encoding="utf-8",
    )

    with pytest.raises(module.CloudflareTunnelManagerError, match="drifted"):
        manager.install()

    assert not runner.calls
