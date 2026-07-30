from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "docker_public.sh"
PUBLIC_DOCKER_ENTRYPOINTS = (
    ROOT / "scripts" / "test_update.sh",
    ROOT / "scripts" / "verify_all.sh",
    ROOT / "scripts" / "build_g2_images.sh",
    ROOT / "scripts" / "verify_vendor_live_test.sh",
    ROOT / "scripts" / "verify_milestone.sh",
    ROOT / "scripts" / "verify_release_control.sh",
    ROOT / "scripts" / "verify_data_images.sh",
)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fake_docker_environment(
    tmp_path: Path,
    *,
    endpoint: str = "unix:///tmp/fake-docker.sock",
) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    plugin_dir = tmp_path / "plugins"
    fake_bin.mkdir()
    plugin_dir.mkdir()
    for plugin in ("docker-buildx", "docker-compose"):
        _write_executable(plugin_dir / plugin, "#!/usr/bin/env sh\nexit 0\n")

    docker_log = tmp_path / "docker.jsonl"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if args == ["context", "show"]:
    print("orbstack")
elif args[:2] == ["context", "inspect"]:
    print(os.environ["FAKE_DOCKER_ENDPOINT"])
elif args == ["info", "--format", "{{json .ClientInfo.Plugins}}"]:
    print(json.dumps([
        {"Name": "buildx", "Path": os.environ["FAKE_BUILDX_PATH"]},
        {"Name": "compose", "Path": os.environ["FAKE_COMPOSE_PATH"]},
    ]))
elif args == ["version"]:
    print("fake docker version")
elif args == ["compose", "version"]:
    print("fake compose version")
elif args == ["buildx", "version"]:
    print("fake buildx version")
elif args == [
    "buildx",
    "imagetools",
    "inspect",
    "docker.io/library/alpine:3.22",
]:
    print("fake manifest")
else:
    print("unexpected fake docker argv", file=sys.stderr)
    raise SystemExit(91)
""",
    )
    keychain_marker = tmp_path / "osxkeychain-called"
    _write_executable(
        fake_bin / "docker-credential-osxkeychain",
        """#!/usr/bin/env sh
: > "$FAKE_OSXKEYCHAIN_MARKER"
exit 97
""",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_ENDPOINT": endpoint,
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_BUILDX_PATH": str(plugin_dir / "docker-buildx"),
        "FAKE_COMPOSE_PATH": str(plugin_dir / "docker-compose"),
        "FAKE_OSXKEYCHAIN_MARKER": str(keychain_marker),
        "DOCKER_AUTH_CONFIG": '{"auths":{"forbidden.example":{"auth":"sentinel"}}}',
        "REGISTRY_AUTH_FILE": str(tmp_path / "forbidden-auth.json"),
    }
    environment.pop("DOCKER_CONFIG", None)
    environment.pop("DOCKER_HOST", None)
    environment.pop("SMS_DOCKER_PUBLIC_SESSION", None)
    return environment, docker_log, keychain_marker


def _run_script(
    tmp_path: Path,
    *args: str,
    endpoint: str = "unix:///tmp/fake-docker.sock",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    environment, docker_log, keychain_marker = _fake_docker_environment(
        tmp_path,
        endpoint=endpoint,
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, docker_log, keychain_marker


def test_public_session_uses_empty_auth_local_socket_plugins_and_cleans_up(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.py"
    output = tmp_path / "probe.json"
    probe.write_text(
        """from __future__ import annotations
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

config_dir = Path(os.environ["DOCKER_CONFIG"])
config = config_dir / "config.json"
docker = subprocess.run(
    ["docker", "version"], check=False, capture_output=True, text=True
)
payload = {
    "session": os.environ.get("SMS_DOCKER_PUBLIC_SESSION"),
    "docker_config": str(config_dir),
    "docker_host": os.environ.get("DOCKER_HOST"),
    "docker_auth_config_present": "DOCKER_AUTH_CONFIG" in os.environ,
    "registry_auth_file_present": "REGISTRY_AUTH_FILE" in os.environ,
    "config": json.loads(config.read_text(encoding="utf-8")),
    "config_dir_mode": stat.S_IMODE(config_dir.stat().st_mode),
    "config_mode": stat.S_IMODE(config.stat().st_mode),
    "docker_returncode": docker.returncode,
}
Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")
""",
        encoding="utf-8",
    )
    result, docker_log, keychain_marker = _run_script(
        tmp_path,
        "run",
        "--",
        "python3",
        str(probe),
        str(output),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["session"] == "1"
    assert payload["docker_host"] == "unix:///tmp/fake-docker.sock"
    assert payload["docker_auth_config_present"] is False
    assert payload["registry_auth_file_present"] is False
    assert payload["config"] == {
        "auths": {},
        "cliPluginsExtraDirs": [str(tmp_path / "plugins")],
        "credsStore": "sms-public",
    }
    assert payload["config_dir_mode"] == 0o700
    assert payload["config_mode"] == 0o600
    assert payload["docker_returncode"] == 0
    assert not Path(payload["docker_config"]).exists()
    assert not keychain_marker.exists()
    calls = [
        json.loads(line)
        for line in docker_log.read_text(encoding="utf-8").splitlines()
    ]
    assert ["version"] in calls


def test_public_session_propagates_child_exit_code_and_still_cleans_up(
    tmp_path: Path,
) -> None:
    output = tmp_path / "config-path"
    command = 'printf %s "$DOCKER_CONFIG" > "$1"; exit 23'
    result, _, _ = _run_script(
        tmp_path,
        "run",
        "--",
        "bash",
        "-c",
        command,
        "probe",
        str(output),
    )

    assert result.returncode == 23
    assert not Path(output.read_text(encoding="utf-8")).exists()


def test_public_session_restores_callers_umask_for_child_command(tmp_path: Path) -> None:
    environment, _, _ = _fake_docker_environment(tmp_path)
    launcher = tmp_path / "launcher.sh"
    output = tmp_path / "child-umask"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
umask 0022
exec bash "$1" run -- bash -c 'umask > "$1"' probe "$2"
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    result = subprocess.run(
        [str(launcher), str(SCRIPT), str(output)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").strip() in {"0022", "022"}


def test_public_session_rejects_non_local_docker_endpoint(tmp_path: Path) -> None:
    marker = tmp_path / "child-ran"
    result, _, _ = _run_script(
        tmp_path,
        "run",
        "--",
        "touch",
        str(marker),
        endpoint="ssh://builder.example",
    )

    assert result.returncode != 0
    assert "local unix Docker endpoint is required" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    "docker_args",
    [
        ("login", "docker.io"),
        ("logout", "docker.io"),
        ("push", "example/image:tag"),
        ("build", "--push", "."),
        ("buildx", "build", "--output=type=registry", "."),
        ("buildx", "build", "-o", "type=registry", "."),
        ("buildx", "build", "--output=type=image,push=true", "."),
        ("buildx", "build", "-o", "type=image,push=true", "."),
        ("buildx", "imagetools", "create", "example/image:tag"),
        ("--config", "/tmp/override", "version"),
        ("--context", "remote", "version"),
        ("--host", "tcp://example:2375", "version"),
    ],
)
def test_public_session_docker_shim_rejects_authenticated_or_override_operations(
    tmp_path: Path,
    docker_args: tuple[str, ...],
) -> None:
    result, docker_log, _ = _run_script(
        tmp_path,
        "run",
        "--",
        "docker",
        *docker_args,
    )

    assert result.returncode != 0
    assert "authenticated registry operation is forbidden" in result.stderr
    calls = [
        json.loads(line)
        for line in docker_log.read_text(encoding="utf-8").splitlines()
    ]
    assert list(docker_args) not in calls


def test_public_session_cleans_up_when_process_group_receives_term(tmp_path: Path) -> None:
    environment, _, _ = _fake_docker_environment(tmp_path)
    output = tmp_path / "config-path"
    process = subprocess.Popen(
        [
            "bash",
            str(SCRIPT),
            "run",
            "--",
            "bash",
            "-c",
            'printf %s "$DOCKER_CONFIG" > "$1"; sleep 30',
            "probe",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    for _ in range(100):
        if output.exists():
            break
        time.sleep(0.02)
    assert output.exists()
    session_dir = Path(output.read_text(encoding="utf-8"))

    os.killpg(process.pid, signal.SIGTERM)
    _stdout, _stderr = process.communicate(timeout=5)

    assert process.returncode in {143, -signal.SIGTERM}
    assert not session_dir.exists()


def test_public_credential_helper_never_stores_or_echoes_input(tmp_path: Path) -> None:
    probe = tmp_path / "credential_probe.py"
    output = tmp_path / "credential_probe.json"
    probe.write_text(
        """from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

helper = shutil.which("docker-credential-sms-public")
assert helper is not None
results = {}
for action in ("get", "list", "store", "erase"):
    completed = subprocess.run(
        [helper, action],
        input="registry-and-secret-sentinel\\n",
        check=False,
        capture_output=True,
        text=True,
    )
    results[action] = {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
Path(sys.argv[1]).write_text(json.dumps(results), encoding="utf-8")
""",
        encoding="utf-8",
    )
    result, _, keychain_marker = _run_script(
        tmp_path,
        "run",
        "--",
        "python3",
        str(probe),
        str(output),
    )

    assert result.returncode == 0, result.stderr
    results = json.loads(output.read_text(encoding="utf-8"))
    assert results["get"] == {
        "returncode": 1,
        "stdout": "credentials not found in native keychain\n",
        "stderr": "",
    }
    assert results["list"] == {"returncode": 0, "stdout": "{}\n", "stderr": ""}
    for action in ("store", "erase"):
        assert results[action]["returncode"] != 0
        assert "registry-and-secret-sentinel" not in results[action]["stdout"]
        assert "registry-and-secret-sentinel" not in results[action]["stderr"]
    assert not keychain_marker.exists()


def test_doctor_checks_daemon_plugins_registry_and_cleanup(tmp_path: Path) -> None:
    result, docker_log, keychain_marker = _run_script(tmp_path, "doctor")

    assert result.returncode == 0, result.stderr
    for capability in ("daemon", "compose", "buildx", "registry-metadata", "cleanup"):
        assert f"docker-public: {capability} PASS" in result.stdout
    calls = [
        json.loads(line)
        for line in docker_log.read_text(encoding="utf-8").splitlines()
    ]
    assert ["version"] in calls
    assert ["compose", "version"] in calls
    assert ["buildx", "version"] in calls
    assert [
        "buildx",
        "imagetools",
        "inspect",
        "docker.io/library/alpine:3.22",
    ] in calls
    assert not keychain_marker.exists()


@pytest.mark.parametrize("args", [(), ("run",), ("run", "--"), ("unknown",)])
def test_public_session_rejects_invalid_wrapper_invocation(
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    result, _, _ = _run_script(tmp_path, *args)

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_public_script_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    assert stat.S_IMODE(SCRIPT.stat().st_mode) == 0o755


def test_development_docker_entrypoints_reexec_once_through_public_session() -> None:
    invocation = 'scripts/docker_public.sh" run -- bash "$0" "$@"'

    for entrypoint in PUBLIC_DOCKER_ENTRYPOINTS:
        source = entrypoint.read_text(encoding="utf-8")
        assert '${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1"' in source, entrypoint
        assert invocation in source, entrypoint
        assert source.count(invocation) == 1, entrypoint


def test_development_docker_entrypoints_do_not_override_isolation_or_use_absolute_docker(
) -> None:
    for entrypoint in PUBLIC_DOCKER_ENTRYPOINTS:
        source = entrypoint.read_text(encoding="utf-8")
        assert "DOCKER_CONFIG=" not in source, entrypoint
        assert "DOCKER_AUTH_CONFIG=" not in source, entrypoint
        assert "REGISTRY_AUTH_FILE=" not in source, entrypoint
        assert "/usr/local/bin/docker" not in source, entrypoint
        assert "/opt/homebrew/bin/docker" not in source, entrypoint


def test_local_test_wraps_only_commands_that_use_docker() -> None:
    source = (ROOT / "scripts" / "local_test.sh").read_text(encoding="utf-8")
    bootstrap = source.split("public_docker_session() {", 1)[1].split("\n}", 1)[0]

    assert "up|status|down|reset" in bootstrap
    assert "prepare" not in bootstrap
    assert "help" not in bootstrap
    assert '${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1"' in bootstrap
    assert 'scripts/docker_public.sh" run -- bash "$0" "$@"' in bootstrap
