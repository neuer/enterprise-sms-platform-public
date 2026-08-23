from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

from classify_ci_changes import classify_paths  # noqa: E402
from protected_path_policy import (  # noqa: E402
    BACKEND_CRITICAL_RAISE_EXACT,
    REQUIRED_CODEOWNERS_PATTERNS,
    REVIEWED_ORDINARY_EXACT,
    security_domain_category,
    unclassified_security_domain_paths,
)
from test_update_contract import (  # noqa: E402
    classify_changed_paths,
    protected_change_category,
)

FRONTEND_SESSION_SECURITY_PATHS = (
    "frontend/src/api/auth.ts",
    "frontend/src/api/refreshLock.ts",
    "frontend/src/api/webMessages.ts",
    "frontend/src/stores/session.ts",
    "frontend/src/api/sessionTokens.ts",
    "frontend/src/api/sessionGeneration.ts",
)


def tracked_files() -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def test_issue_427_paths_are_classified_full_and_not_skipped() -> None:
    raw = classify_paths(["backend/app/services/raw_capture_legacy.py"])
    ops = classify_paths(["backend/app/services/ops_repository.py"])
    app = classify_paths(["frontend/src/App.vue"])

    assert (raw.backend, raw.g2, raw.security, raw.full_fallback) == (True, True, True, False)
    assert raw.categories == frozenset({"backend-critical"})
    assert (ops.backend, ops.g2, ops.security, ops.full_fallback) == (True, True, True, False)
    assert ops.categories == frozenset({"backend-critical"})
    assert (app.frontend, app.security, app.g2, app.full_fallback) == (True, True, False, False)
    assert app.categories == frozenset({"frontend-security"})


@pytest.mark.parametrize("path", FRONTEND_SESSION_SECURITY_PATHS)
def test_issue_454_session_paths_are_frontend_security_not_backend_critical(
    path: str,
) -> None:
    result = classify_paths([path])

    assert path not in BACKEND_CRITICAL_RAISE_EXACT
    assert path not in REVIEWED_ORDINARY_EXACT
    assert security_domain_category(path) == "frontend-security"
    assert protected_change_category(path) == "frontend-security"
    assert (result.backend, result.frontend, result.g2, result.security) == (
        False,
        True,
        False,
        True,
    )
    assert result.categories == frozenset({"frontend-security"})
    assert result.full_fallback is False


def test_ops_repository_uses_existing_backend_critical_postgres_gate() -> None:
    """ops 仓储走既有 backend-critical，由 backend/G2 附带真实 PostgreSQL 测试。"""

    assert protected_change_category("backend/app/services/ops_repository.py") == (
        "backend-critical"
    )
    scope = classify_changed_paths(["backend/app/services/ops_repository.py"])
    assert scope.risk == "high-risk"
    assert scope.components == frozenset({"api"})
    ci_yml = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    postgres_gate = (ROOT / "scripts" / "verify_vendor_postgres_recovery.sh").read_text(
        encoding="utf-8"
    )
    assert "SMS_COVERAGE=1 bash ../scripts/verify_vendor_postgres_recovery.sh" in ci_yml
    assert "test_raw_capture_legacy_postgres.py" in postgres_gate
    assert "SECURITY_SESSION_POSTGRES_DSN" in postgres_gate


def test_app_vue_uses_existing_frontend_security_gate() -> None:
    scope = classify_changed_paths(["frontend/src/App.vue"])
    assert scope.risk == "high-risk"
    assert scope.components == frozenset({"web"})
    frontend_job = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "npm test" in frontend_job
    assert (ROOT / "frontend" / "tests" / "app-shell.test.ts").is_file()


def test_codeowners_lists_every_required_security_domain() -> None:
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for pattern in REQUIRED_CODEOWNERS_PATTERNS:
        assert f"{pattern} @neuer" in codeowners


def test_tracked_security_domain_files_are_classified() -> None:
    missing = unclassified_security_domain_paths(tracked_files())
    assert missing == ()
    for relative in REVIEWED_ORDINARY_EXACT | BACKEND_CRITICAL_RAISE_EXACT:
        assert relative in tracked_files()


def test_reviewed_ordinary_is_an_explicit_downgrade() -> None:
    assert security_domain_category("backend/app/services/dashboard.py") is None
    assert protected_change_category("backend/app/services/dashboard.py") is None
    assert "backend/app/services/raw_capture_legacy.py" not in REVIEWED_ORDINARY_EXACT
    assert "backend/app/services/ops_repository.py" not in REVIEWED_ORDINARY_EXACT
    assert "frontend/src/App.vue" not in REVIEWED_ORDINARY_EXACT


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("backend/app/services/brand_new_ledger.py", "backend-critical"),
        ("frontend/src/stores/brand_new_session.ts", "frontend-security"),
        ("frontend/src/App.vue", "frontend-security"),
    ],
)
def test_domain_default_does_not_require_an_exact_filename(
    path: str,
    category: str,
) -> None:
    assert security_domain_category(path) == category
    assert protected_change_category(path) == category
