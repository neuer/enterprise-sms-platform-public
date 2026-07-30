from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import install_vendor_credentials as installer  # noqa: E402


class FakeTTY:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def isatty(self) -> bool:
        return self.enabled


def test_installer_reads_both_values_only_from_tty_and_writes_canonical_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "secrets"
    source.mkdir(mode=0o700)
    prompts: list[str] = []
    values = iter(["formal-name", "formal-key"])

    def reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(values)

    installed = installer.install_vendor_credentials(
        source,
        reader=reader,
        stdin=FakeTTY(True),  # type: ignore[arg-type]
        stdout=FakeTTY(True),  # type: ignore[arg-type]
        expected_uid=os.geteuid(),
    )

    assert installed is True
    assert prompts == ["SecretName: ", "SecretKey: "]
    assert (source / "vendor_secret_name").read_text() == "formal-name"
    assert (source / "vendor_secret_key").read_text() == "formal-key"
    assert stat.S_IMODE((source / "vendor_secret_name").stat().st_mode) == 0o600
    assert stat.S_IMODE((source / "vendor_secret_key").stat().st_mode) == 0o600
    assert not [path for path in source.iterdir() if path.name.startswith(".")]


def test_installer_rejects_non_tty_without_reading_values(tmp_path: Path) -> None:
    calls = 0

    def reader(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "must-not-read"

    with pytest.raises(installer.VendorCredentialInstallError, match="TTY"):
        installer.install_vendor_credentials(
            tmp_path / "secrets",
            reader=reader,
            stdin=FakeTTY(False),  # type: ignore[arg-type]
            stdout=FakeTTY(True),  # type: ignore[arg-type]
            expected_uid=os.geteuid(),
        )

    assert calls == 0


def test_installer_output_never_contains_values_length_digest_or_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "secrets"
    source.mkdir(mode=0o700)
    values = iter(["formal-name-private", "formal-key-private"])
    monkeypatch.setattr(
        installer,
        "require_interactive_tty",
        lambda _stdin, _stdout: None,
    )
    monkeypatch.setattr(installer.getpass, "getpass", lambda _prompt: next(values))
    monkeypatch.setattr(installer, "CANONICAL_SOURCE_DIR", source)
    monkeypatch.setattr(installer, "EXPECTED_ROOT_UID", os.geteuid())

    assert installer.main() == 0

    output = capsys.readouterr().out
    assert output.strip() == "已安装"
    assert "formal" not in output
    assert "length" not in output.casefold()
    assert "hash" not in output.casefold()


def test_installer_rejects_unsafe_source_directory_or_empty_value(tmp_path: Path) -> None:
    source = tmp_path / "secrets"
    source.mkdir(mode=0o755)
    with pytest.raises(installer.VendorCredentialInstallError, match="source directory"):
        installer.install_vendor_credentials(
            source,
            reader=lambda _prompt: "value",
            stdin=FakeTTY(True),  # type: ignore[arg-type]
            stdout=FakeTTY(True),  # type: ignore[arg-type]
            expected_uid=os.geteuid(),
        )

    source.chmod(0o700)
    values = iter(["", "unused"])
    with pytest.raises(installer.VendorCredentialInstallError, match="not installed"):
        installer.install_vendor_credentials(
            source,
            reader=lambda _prompt: next(values),
            stdin=FakeTTY(True),  # type: ignore[arg-type]
            stdout=FakeTTY(True),  # type: ignore[arg-type]
            expected_uid=os.geteuid(),
        )
