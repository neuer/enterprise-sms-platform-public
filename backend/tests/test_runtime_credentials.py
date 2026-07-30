from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runtime_credentials import read_secret_file  # noqa: E402


def test_runtime_secret_reader_requires_owner_only_nonempty_single_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mock_password"
    path.write_text("runtime-only-password\n", encoding="utf-8")
    path.chmod(0o600)

    assert read_secret_file(path, label="mock password") == "runtime-only-password"

    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        read_secret_file(path, label="mock password")

    path.chmod(0o600)
    path.write_text("first\nsecond\n", encoding="utf-8")
    with pytest.raises(ValueError, match="one line"):
        read_secret_file(path, label="mock password")


def test_runtime_secret_reader_never_echoes_secret_in_errors(tmp_path: Path) -> None:
    path = tmp_path / "mock_password"
    path.write_text("must-not-appear\nsecond-line\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ValueError) as captured:
        read_secret_file(path, label="mock password")

    assert "must-not-appear" not in str(captured.value)
