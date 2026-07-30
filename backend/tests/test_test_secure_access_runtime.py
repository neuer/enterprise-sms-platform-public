from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


def runtime() -> ModuleType:
    try:
        return importlib.import_module("test_secure_access_runtime")
    except ModuleNotFoundError:
        pytest.fail("test secure access runtime is not implemented")


class FakeProcess:
    def __init__(
        self,
        lines: list[str],
        *,
        returncode: int = 0,
        on_wait: Any = None,
    ) -> None:
        self.stderr = iter(lines)
        self.returncode = returncode
        self.on_wait = on_wait
        self.terminated = False

    def wait(self) -> int:
        if self.on_wait is not None:
            self.on_wait()
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


class ProcessFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        self.calls.append((argv, kwargs))
        return self.process


def test_runtime_builds_one_fixed_cloudflared_command() -> None:
    module = runtime()

    assert module.cloudflared_argv() == (
        "/usr/local/libexec/sms-platform/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--protocol",
        "http2",
        "--url",
        "http://127.0.0.1:18080",
    )


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["https://unsafe.example.test\n"],
        [
            "https://first-safe.trycloudflare.com\n",
            "https://second-safe.trycloudflare.com\n",
        ],
    ],
)
def test_runtime_rejects_missing_invalid_or_multiple_tunnel_urls(
    lines: list[str],
) -> None:
    module = runtime()

    with pytest.raises(module.SecureAccessRuntimeError, match="tunnel URL"):
        module.extract_quick_tunnel_url(lines)


def test_runtime_extracts_repeated_identical_url_from_mixed_logs() -> None:
    module = runtime()
    url = "https://safe-name.trycloudflare.com"

    assert (
        module.extract_quick_tunnel_url(
            [
                "fixed cloudflared startup text\n",
                f"INF Your quick Tunnel has been created! Visit it at {url}\n",
                f"INF route ready {url}\n",
            ]
        )
        == url
    )


def test_runtime_does_not_extract_a_valid_prefix_from_an_attacker_hostname() -> None:
    module = runtime()

    with pytest.raises(module.SecureAccessRuntimeError, match="tunnel URL"):
        module.extract_quick_tunnel_url(
            ["https://safe-name.trycloudflare.com.evil.example\n"]
        )


def test_runtime_atomically_writes_private_ready_state(tmp_path: Path) -> None:
    module = runtime()
    status_path = tmp_path / "runtime/status.json"
    started_at = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)

    module.write_ready_state(
        status_path,
        url="https://safe-name.trycloudflare.com",
        started_at=started_at,
    )

    assert status_path.stat().st_mode & 0o777 == 0o640
    assert json.loads(status_path.read_text(encoding="utf-8"))["expires_at"] == (
        "2026-07-19T09:15:00+00:00"
    )
    assert list(status_path.parent.glob(".*.tmp")) == []


def test_runtime_keeps_state_only_while_fixed_process_is_running(tmp_path: Path) -> None:
    module = runtime()
    status_path = tmp_path / "runtime/status.json"
    url = "https://safe-name.trycloudflare.com"

    def assert_ready() -> None:
        assert json.loads(status_path.read_text(encoding="utf-8"))["url"] == url

    process = FakeProcess([f"INF quick tunnel {url}\n"], on_wait=assert_ready)
    factory = ProcessFactory(process)
    runner = module.QuickTunnelRuntime(
        status_path=status_path,
        process_factory=factory,
        clock=lambda: datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
    )

    assert runner.run() == 0
    assert not status_path.exists()
    assert len(factory.calls) == 1
    argv, kwargs = factory.calls[0]
    assert argv == module.cloudflared_argv()
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "close_fds": True,
        "env": {
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    }


def test_runtime_cleans_state_and_hides_raw_logs_on_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = runtime()
    status_path = tmp_path / "runtime/status.json"
    raw_log = "credential-like-sentinel"
    process = FakeProcess([raw_log], returncode=1)
    runner = module.QuickTunnelRuntime(
        status_path=status_path,
        process_factory=ProcessFactory(process),
        clock=lambda: datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
    )

    with pytest.raises(module.SecureAccessRuntimeError):
        runner.run()

    assert not status_path.exists()
    captured = capsys.readouterr()
    assert raw_log not in captured.out
    assert raw_log not in captured.err


def test_runtime_terminates_process_if_a_second_url_appears_after_ready(
    tmp_path: Path,
) -> None:
    module = runtime()
    status_path = tmp_path / "runtime/status.json"
    process = FakeProcess(
        [
            "https://first-safe.trycloudflare.com\n",
            "https://second-safe.trycloudflare.com\n",
        ]
    )
    runner = module.QuickTunnelRuntime(
        status_path=status_path,
        process_factory=ProcessFactory(process),
        clock=lambda: datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
    )

    with pytest.raises(module.SecureAccessRuntimeError, match="tunnel URL"):
        runner.run()

    assert process.terminated is True
    assert not status_path.exists()
