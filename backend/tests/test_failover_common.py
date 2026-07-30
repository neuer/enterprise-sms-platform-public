from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from failover_common import (  # noqa: E402
    CommandFailure,
    CommandRunner,
    atomic_write_json,
    sha256_file,
    validate_drill_database,
    validate_passphrase_file,
    validate_remote,
)


def test_passphrase_must_be_external_regular_0600_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / "backup-passphrase"
    external.write_text("not-a-real-secret", encoding="utf-8")
    external.chmod(0o600)

    assert validate_passphrase_file(external, root) == external.resolve()

    external.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        validate_passphrase_file(external, root)
    internal = root / "passphrase"
    internal.write_text("x", encoding="utf-8")
    internal.chmod(0o600)
    with pytest.raises(ValueError, match="仓库外"):
        validate_passphrase_file(internal, root)
    link = tmp_path / "passphrase-link"
    link.symlink_to(internal)
    with pytest.raises(ValueError, match="符号链接"):
        validate_passphrase_file(link, root)


@pytest.mark.parametrize(
    ("host", "user", "root"),
    [
        ("backup01.internal", "smsdr", "/srv/sms-standby"),
        ("10.0.0.8", "sms_dr", "/srv/sms_platform/releases"),
    ],
)
def test_remote_identity_accepts_only_bounded_values(host: str, user: str, root: str) -> None:
    assert validate_remote(host, user, root) == (host, user, root)


@pytest.mark.parametrize(
    ("host", "user", "root"),
    [
        ("host;touch /tmp/pwn", "smsdr", "/srv/sms"),
        ("backup", "root $(id)", "/srv/sms"),
        ("backup", "smsdr", "../../etc"),
        ("backup", "smsdr", "/srv/../etc"),
    ],
)
def test_remote_identity_rejects_shell_and_path_injection(host: str, user: str, root: str) -> None:
    with pytest.raises(ValueError):
        validate_remote(host, user, root)


def test_drill_database_can_never_name_production() -> None:
    assert validate_drill_database("sms_drill_20260712_ab12") == "sms_drill_20260712_ab12"
    for unsafe in ("sms", "postgres", "sms_drill_x;drop", "SMS_DRILL_X"):
        with pytest.raises(ValueError):
            validate_drill_database(unsafe)


def test_sha256_and_atomic_json_are_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "artifact.enc"
    source.write_bytes(b"ciphertext")
    destination = tmp_path / "manifest.json"

    assert sha256_file(source) == "305531dcc50ebca31cf1d5b31e9fc76ed51f66b3b6dd5a030c6539ae6532f979"
    atomic_write_json(destination, {"sha256": sha256_file(source), "ok": True})

    assert json.loads(destination.read_text(encoding="utf-8"))["ok"] is True
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_runner_pipeline_streams_without_shell(tmp_path: Path) -> None:
    output = tmp_path / "output.bin"
    runner = CommandRunner()

    runner.pipeline_to_file(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'abc')"],
        [
            sys.executable,
            "-c",
            "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read().upper())",
        ],
        output,
    )

    assert output.read_bytes() == b"ABC"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_runner_removes_partial_output_and_redacts_failure(tmp_path: Path) -> None:
    output = tmp_path / "partial.enc"
    runner = CommandRunner()

    with pytest.raises(CommandFailure) as captured:
        runner.pipeline_to_file(
            [sys.executable, "-c", "import sys;sys.stderr.write('secret=abc');sys.exit(4)"],
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            output,
        )

    assert not output.exists()
    assert "abc" not in str(captured.value)
    assert "secret=<redacted>" in str(captured.value)
    shell = os.environ.get("SHELL")
    assert shell is None or shell not in str(captured.value)
