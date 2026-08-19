from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"


def load_migration(path: Path, name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_approval_threshold_snapshot_has_schema_and_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0020_approval_threshold_snapshot.py"
    compatibility_revision = (
        BACKEND / "migrations/versions/0021_approval_legacy_writer_default.py"
    )

    assert "trigger_threshold INTEGER" in schema
    assert "trigger_threshold_source" in schema
    assert revision.is_file()
    spec = importlib.util.spec_from_file_location("approval_threshold_snapshot", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0019_vendor_hmac_alias"
    migration = revision.read_text(encoding="utf-8")
    assert "legacy_unknown" in migration
    assert "SELECT value::integer FROM sys_config" not in migration
    assert compatibility_revision.is_file()
    compatibility = load_migration(
        compatibility_revision,
        "approval_legacy_writer_default",
    )
    assert compatibility.down_revision == "0020_approval_threshold"


def test_openapi_exposes_qingluan_runtime_and_approval_facts() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    dashboard = schemas["DashboardModel"]
    assert "ui_policy" in set(dashboard["required"])
    assert dashboard["properties"]["operations"]["anyOf"][0]["$ref"].endswith(
        "/DashboardOperationsModel"
    )
    assert "channel_monitor" in set(
        schemas["DashboardOperationsModel"]["required"]
    )
    approval = schemas["ApprovalListItem"]
    assert {
        "segments",
        "estimated_segments",
        "scheduled_at",
        "trigger_threshold",
        "trigger_threshold_source",
    } <= set(approval["required"])
    threshold_types = approval["properties"]["trigger_threshold"]["anyOf"]
    assert {item["type"] for item in threshold_types} == {"integer", "null"}
