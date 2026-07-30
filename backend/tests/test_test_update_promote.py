from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(DEPLOY_SCRIPTS))

import test_update_promote as module  # noqa: E402

BASE = "1" * 40
TARGET = "2" * 40
TREE = "3" * 40


def test_promote_only_moves_identity_when_trees_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "CANONICAL_ROOT", tmp_path)
    marker_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        module,
        "require_test_host_marker",
        lambda *a, **k: marker_calls.append((a, k)),
    )
    monkeypatch.setattr(
        module,
        "require_inherited_lifecycle_lock",
        lambda *a, **k: None,
    )
    state = {"head": BASE, "checked_out": False}

    def fake_command(*arguments: str, root: Path) -> str:
        assert root == tmp_path
        if arguments == ("git", "status", "--porcelain"):
            return ""
        if arguments == ("git", "rev-parse", "HEAD"):
            return str(state["head"])
        if arguments == ("git", "rev-parse", "refs/test-updates/promote/source^{commit}"):
            return TARGET
        if arguments in {
            ("git", "rev-parse", f"{BASE}^{{tree}}"),
            ("git", "rev-parse", f"{TARGET}^{{tree}}"),
        }:
            return TREE
        if arguments[:4] == ("git", "fetch", "--prune", "--no-tags"):
            return ""
        if arguments == ("git", "checkout", "--detach", TARGET):
            state["head"] = TARGET
            state["checked_out"] = True
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "command", fake_command)

    result = module.promote(
        root=tmp_path,
        source_ref="origin/main",
        target=TARGET,
    )

    assert state["checked_out"] is True
    assert result["state"] == "promoted"
    assert result["base_commit"] == BASE
    assert result["commit"] == TARGET
    assert marker_calls == [((), {"expected_uid": 0})]


def test_promote_rejects_different_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "CANONICAL_ROOT", tmp_path)
    marker_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        module,
        "require_test_host_marker",
        lambda *a, **k: marker_calls.append((a, k)),
    )
    monkeypatch.setattr(
        module,
        "require_inherited_lifecycle_lock",
        lambda *a, **k: None,
    )

    def fake_command(*arguments: str, root: Path) -> str:
        assert root == tmp_path
        if arguments == ("git", "status", "--porcelain"):
            return ""
        if arguments == ("git", "rev-parse", "HEAD"):
            return BASE
        if arguments == ("git", "rev-parse", "refs/test-updates/promote/source^{commit}"):
            return TARGET
        if arguments == ("git", "rev-parse", f"{BASE}^{{tree}}"):
            return TREE
        if arguments == ("git", "rev-parse", f"{TARGET}^{{tree}}"):
            return "4" * 40
        if arguments[:4] == ("git", "fetch", "--prune", "--no-tags"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "command", fake_command)

    with pytest.raises(module.TestUpdatePromoteError, match="identical Git trees"):
        module.promote(
            root=tmp_path,
            source_ref="origin/main",
            target=TARGET,
        )
    assert marker_calls == [((), {"expected_uid": 0})]
