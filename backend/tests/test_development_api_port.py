from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "development_api_port_contract", ROOT / "deploy/scripts/development_api_port.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("mode", ["development", "production"])
def test_no_opt_in_does_not_publish_or_require_environment_file(tmp_path: Path, mode: str) -> None:
    assert not MODULE.enabled(tmp_path / "missing", mode=mode, environ={})


@pytest.mark.parametrize("value", ["development", "'development'", '"development"'])
def test_explicit_development_opt_in(tmp_path: Path, value: str) -> None:
    path = tmp_path / "env"
    path.write_text(f"ENVIRONMENT={value}\nUNRELATED=value\n")
    assert MODULE.enabled(path, mode="development", environ={"SMS_DEV_API_PORT": "1"})


@pytest.mark.parametrize("content", [
    "", "ENVIRONMENT=test", "ENVIRONMENT=production", "ENVIRONMENT=unknown",
    "ENVIRONMENT=development\nENVIRONMENT=development",
    "ENVIRONMENT=development\nENVIRONMENT=production", "ENVIRONMENT=${ENVIRONMENT}",
    "ENVIRONMENT=development\nENVIRONMENT: test",
    "ENVIRONMENT=development\nENVIRONMENT",
    "ENVIRONMENT=development\nexport ENVIRONMENT: production",
    "ENVIRONMENT: development",
    "OTHER='\nENVIRONMENT=development\n'",
    'OTHER="\nENVIRONMENT=development\n"',
    "ENVIRONMENT=development#test",
])
def test_real_env_file_cannot_be_overridden_by_shell(tmp_path: Path, content: str) -> None:
    path = tmp_path / "env"
    path.write_text(content)
    with pytest.raises(ValueError):
        MODULE.enabled(path, mode="development", environ={
            "SMS_DEV_API_PORT": "1", "ENVIRONMENT": "development",
        })


@pytest.mark.parametrize("environ,mode", [
    ({"SMS_DEV_API_PORT": "1"}, "production"),
    ({"SMS_DEV_API_PORT": "true"}, "development"),
    ({"SMS_DEV_API_PORT": "1", "ENVIRONMENT": "test"}, "development"),
])
def test_invalid_mode_or_conflicting_shell_is_rejected(
    tmp_path: Path, environ: dict[str, str], mode: str
) -> None:
    path = tmp_path / "env"
    path.write_text("ENVIRONMENT=development\n")
    with pytest.raises(ValueError):
        MODULE.enabled(path, mode=mode, environ=environ)
