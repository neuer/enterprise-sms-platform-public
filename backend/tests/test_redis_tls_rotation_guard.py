from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
GUARD = SCRIPTS / "redis_tls_rotation_guard.py"


def load_guard() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("redis_tls_rotation_guard", GUARD)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_redis_tls_rotation_guard_exists() -> None:
    assert GUARD.is_file()


def test_guard_rejects_non_root_before_tuple_inspection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_guard()
    monkeypatch.setattr(module.os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        module,
        "verify_ordinary_redis_tls_rotation",
        lambda **_kwargs: pytest.fail("tuple inspection must not run as non-root"),
    )

    result = module.main(
        [
            "--source-dir",
            "/opt/sms-platform/deploy/secrets",
            "--runtime-root",
            "/run/sms-platform/secrets",
            "--baseline-target",
            "generations/generation-00000000000000000000000000000000",
        ]
    )

    assert result == 1
    assert "requires root" in capsys.readouterr().err


def test_guard_delegates_only_fixed_tuple_inputs_and_discloses_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_guard()
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        module,
        "verify_ordinary_redis_tls_rotation",
        lambda **kwargs: observed.append(kwargs),
    )
    baseline = "generations/generation-11111111111111111111111111111111"

    result = module.main(
        [
            "--source-dir",
            "/opt/sms-platform/deploy/secrets",
            "--runtime-root",
            "/run/sms-platform/secrets",
            "--baseline-target",
            baseline,
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert observed == [
        {
            "source_dir": Path("/opt/sms-platform/deploy/secrets"),
            "runtime_root": Path("/run/sms-platform/secrets"),
            "baseline_target": baseline,
        }
    ]
    assert captured.out.strip() == "Redis TLS rotation tuple unchanged"
    assert captured.err == ""
    assert "sha256" not in captured.out.casefold()
