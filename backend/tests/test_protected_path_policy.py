from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

from classify_ci_changes import classify_paths  # noqa: E402
from protected_path_policy import (  # noqa: E402
    BACKEND_CRITICAL_DOMAINS,
    BACKEND_CRITICAL_RAISE_EXACT,
    FRONTEND_SECURITY_DOMAINS,
    REQUIRED_CODEOWNERS_PATTERNS,
    REQUIRED_TRACKED_ROOT_GLOBS,
    REQUIRED_TRACKED_SOURCE_TREES,
    REVIEWED_ORDINARY_EXACT,
    REVIEWED_ORDINARY_REASONS,
    parse_codeowners_patterns,
    render_codeowners,
    required_codeowners_patterns,
    security_domain_category,
    unclassified_tracked_source_paths,
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
ISSUE_427_BACKEND_CRITICAL_PATHS = (
    "backend/app/settings.py",
    "backend/app/main.py",
    "backend/app/outbox_dispatcher.py",
    "backend/app/cli.py",
    "backend/app/models/__init__.py",
    "backend/app/models/brand_new_model.py",
)
ISSUE_427_FRONTEND_SECURITY_VIEWS = (
    "frontend/src/views/LoginView.vue",
    "frontend/src/views/PasswordChangeView.vue",
    "frontend/src/views/AppManagementView.vue",
    "frontend/src/views/ApprovalView.vue",
    "frontend/src/views/ConfigView.vue",
)
# T6-02：实际承担特权操作的视图不得低于 frontend-security。
ISSUE_427_CAPABILITY_SECURITY_VIEWS = (
    "frontend/src/views/OpsView.vue",
    "frontend/src/views/UserView.vue",
    "frontend/src/views/BlacklistView.vue",
    "frontend/src/views/CallbackView.vue",
    "frontend/src/views/SecurityDailyView.vue",
    "frontend/src/views/SendView.vue",
    "frontend/src/views/SensitiveWordView.vue",
    "frontend/src/views/SignView.vue",
    "frontend/src/views/TemplateView.vue",
    "frontend/src/views/AuditView.vue",
    "frontend/src/views/BatchView.vue",
    "frontend/src/views/MessageView.vue",
    "frontend/src/views/ReplyView.vue",
    "frontend/src/views/ReportView.vue",
)
SENSITIVE_VIEW_IMPORTS = frozenset(
    {
        "replayRaw",
        "resumeQueue",
        "retryOutboxEvent",
        "triggerJob",
        "createLocalUser",
        "resetLocalPassword",
        "revokeUserSessions",
        "updateUserRole",
        "updateUserStatus",
        "sendWebMessage",
        "uploadPhones",
        "updateSecurityDailyConfiguration",
        "sendSecurityDailyReport",
        "retrySecurityDailyReport",
        "updateConfigs",
        "addBlacklist",
        "deleteBlacklist",
        "retryCallback",
        "addSensitiveWords",
        "deleteSensitiveWord",
        "createSign",
        "updateSign",
        "deleteSign",
        "syncSign",
        "createTemplate",
        "updateTemplate",
        "deleteTemplate",
        "syncTemplate",
        "decryptMessagePhone",
        "cancelBatch",
        "resendFailedBatch",
        "rescheduleBatch",
        "blacklistReply",
        "createDetailExport",
        "issueExportStepUp",
        "downloadExport",
        "rotateAppKey",
        "rotateCallbackSecret",
        "createApp",
        "updateApp",
        "passwordPolicyRequest",
        "changePassword",
        "decideApproval",
        "listApprovals",
        "issueVendorTestStepUp",
        "installVendorCredentials",
        "activateVendorTest",
        "resumeVendorTest",
    }
)
ISSUE_427_FRONTEND_SECURITY_COMPONENTS = (
    "frontend/src/components/DailyPasswordChangeDialog.vue",
    "frontend/src/components/ApprovalList.vue",
)
SECURITY_VIEW_COMPONENT_IMPORT = (
    r"""from\s+["'](?:\.\./components|\./components)/([^"']+\.vue)["']"""
)
ORDINARY_REASON_REQUIRED_FIELDS = ("allowed_apis=", "excluded=", "review=")


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
    assert ci_yml.count("verify_vendor_postgres_recovery.sh") == 1
    assert "test_raw_capture_legacy_postgres.py" in postgres_gate
    assert "test_raw_replay_eligibility_postgres.py" in postgres_gate
    assert "test_raw_replay_fencing_postgres.py" in postgres_gate
    assert "SECURITY_SESSION_POSTGRES_DSN" in postgres_gate


def test_app_vue_uses_existing_frontend_security_gate() -> None:
    scope = classify_changed_paths(["frontend/src/App.vue"])
    assert scope.risk == "high-risk"
    assert scope.components == frozenset({"web"})
    frontend_job = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "npm test" in frontend_job
    assert (ROOT / "frontend" / "tests" / "app-shell.test.ts").is_file()


def test_codeowners_matches_manifest_bidirectionally() -> None:
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert codeowners == render_codeowners()
    assert parse_codeowners_patterns(codeowners) == required_codeowners_patterns()
    assert required_codeowners_patterns() == REQUIRED_CODEOWNERS_PATTERNS
    assert "deploy/scripts/protected_path_policy.py" in required_codeowners_patterns()


def test_tracked_source_files_are_classified_or_explicitly_downgraded() -> None:
    tracked = tracked_files()
    missing = unclassified_tracked_source_paths(tracked)
    assert missing == ()
    for relative in REVIEWED_ORDINARY_EXACT | BACKEND_CRITICAL_RAISE_EXACT:
        assert relative in tracked
    for _relative, reason in REVIEWED_ORDINARY_REASONS.items():
        assert reason.strip()


def test_reverse_enum_does_not_filter_by_manifest_membership_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缩小域清单时，必扫树上的 source 仍必须被报告为未分类。"""

    import protected_path_policy as policy

    monkeypatch.setattr(
        policy,
        "BACKEND_CRITICAL_DOMAINS",
        (
            "backend/app/core/",
            "backend/app/api/",
            "backend/app/services/",
            "backend/app/tasks/",
            "backend/app/vendor/",
        ),
    )
    monkeypatch.setattr(
        policy,
        "FRONTEND_SECURITY_DOMAINS",
        (
            "frontend/src/App.vue",
            "frontend/src/stores/",
            "frontend/src/router/",
            "frontend/src/api/",
        ),
    )

    missing = policy.unclassified_tracked_source_paths(
        [
            "backend/app/settings.py",
            "backend/app/main.py",
            "backend/app/outbox_dispatcher.py",
            "backend/app/models/__init__.py",
            "backend/app/cli.py",
            "frontend/src/views/LoginView.vue",
            "frontend/src/components/DailyPasswordChangeDialog.vue",
            "frontend/src/main.ts",
            "docs/UAT.md",
        ]
    )
    assert "backend/app/settings.py" in missing
    assert "backend/app/outbox_dispatcher.py" in missing
    assert "backend/app/models/__init__.py" in missing
    assert "frontend/src/views/LoginView.vue" in missing
    assert "frontend/src/components/DailyPasswordChangeDialog.vue" in missing
    assert "frontend/src/main.ts" in missing
    assert "docs/UAT.md" not in missing


def test_reviewed_ordinary_is_an_explicit_downgrade() -> None:
    assert security_domain_category("backend/app/services/dashboard.py") is None
    assert protected_change_category("backend/app/services/dashboard.py") is None
    assert "backend/app/services/raw_capture_legacy.py" not in REVIEWED_ORDINARY_EXACT
    assert "backend/app/services/ops_repository.py" not in REVIEWED_ORDINARY_EXACT
    assert "frontend/src/App.vue" not in REVIEWED_ORDINARY_EXACT
    for path in ISSUE_427_FRONTEND_SECURITY_VIEWS:
        assert path not in REVIEWED_ORDINARY_EXACT
    for path in ISSUE_427_CAPABILITY_SECURITY_VIEWS:
        assert path not in REVIEWED_ORDINARY_EXACT
    for path in ISSUE_427_FRONTEND_SECURITY_COMPONENTS:
        assert path not in REVIEWED_ORDINARY_EXACT
    for path in ISSUE_427_BACKEND_CRITICAL_PATHS:
        if path.endswith("brand_new_model.py"):
            continue
        assert path not in REVIEWED_ORDINARY_EXACT


def test_ordinary_reasons_are_unique_and_structured() -> None:
    reasons = list(REVIEWED_ORDINARY_REASONS.values())
    assert len(set(reasons)) == len(reasons)
    for path, reason in REVIEWED_ORDINARY_REASONS.items():
        for field in ORDINARY_REASON_REQUIRED_FIELDS:
            assert field in reason, f"{path} 缺少 {field}"


@pytest.mark.parametrize("path", REVIEWED_ORDINARY_REASONS)
def test_each_explicit_downgrade_has_reason_and_ordinary_gates(path: str) -> None:
    assert REVIEWED_ORDINARY_REASONS[path].strip()
    assert security_domain_category(path) is None
    result = classify_paths([path])
    assert result.security is False
    assert result.g2 is False
    assert result.full_fallback is False
    if path.startswith("frontend/"):
        assert (result.backend, result.frontend) == (False, True)
        assert result.categories == frozenset({"frontend"})
        assert classify_changed_paths([path]).risk == "web-only"
    else:
        assert (result.backend, result.frontend) == (True, False)
        assert result.categories == frozenset({"backend-check"})


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("backend/app/services/brand_new_ledger.py", "backend-critical"),
        ("backend/app/brand_new_root.py", "backend-critical"),
        ("backend/app/models/brand_new_model.py", "backend-critical"),
        ("frontend/src/stores/brand_new_session.ts", "frontend-security"),
        ("frontend/src/views/BrandNewView.vue", "frontend-security"),
        ("frontend/src/components/BrandNewDialog.vue", "frontend-security"),
        ("frontend/src/App.vue", "frontend-security"),
        ("frontend/src/main.ts", "frontend-security"),
        ("frontend/src/brand_new_entry.ts", "frontend-security"),
        ("frontend/src/BrandNewRoot.vue", "frontend-security"),
    ],
)
def test_domain_default_does_not_require_an_exact_filename(
    path: str,
    category: str,
) -> None:
    assert security_domain_category(path) == category
    assert protected_change_category(path) == category


@pytest.mark.parametrize("path", ISSUE_427_BACKEND_CRITICAL_PATHS)
def test_issue_427_backend_root_and_models_select_backend_security_g2(path: str) -> None:
    assert "backend/app/" in BACKEND_CRITICAL_DOMAINS
    assert security_domain_category(path) == "backend-critical"
    result = classify_paths([path])
    assert (result.backend, result.g2, result.security) == (True, True, True)
    assert result.full_fallback is False
    protected = protected_change_category(path)
    assert protected in {"backend-critical", "vendor-live"}


@pytest.mark.parametrize("path", ISSUE_427_FRONTEND_SECURITY_VIEWS)
def test_issue_427_sensitive_views_are_frontend_security(path: str) -> None:
    assert "frontend/src/views/" in FRONTEND_SECURITY_DOMAINS
    assert security_domain_category(path) == "frontend-security"
    result = classify_paths([path])
    assert result.frontend is True
    assert result.security is True
    assert result.full_fallback is False
    if path == "frontend/src/views/ConfigView.vue":
        assert protected_change_category(path) == "vendor-live"
        assert (result.backend, result.g2) == (True, True)
    else:
        assert protected_change_category(path) == "frontend-security"
        assert (result.backend, result.g2) == (False, False)
        assert result.categories == frozenset({"frontend-security"})
        assert classify_changed_paths([path]).risk == "high-risk"


@pytest.mark.parametrize("path", ISSUE_427_CAPABILITY_SECURITY_VIEWS)
def test_sensitive_view_classification_is_not_below_capability(path: str) -> None:
    """分类已经存在但低于实际安全能力时必须失败。"""

    assert path not in REVIEWED_ORDINARY_EXACT
    assert security_domain_category(path) == "frontend-security"
    result = classify_paths([path])
    assert result.frontend is True
    assert result.security is True
    assert result.g2 is False
    assert result.full_fallback is False
    assert result.categories == frozenset({"frontend-security"})
    assert classify_changed_paths([path]).risk == "high-risk"


def test_ordinary_views_do_not_import_sensitive_apis() -> None:
    for path, _reason in REVIEWED_ORDINARY_REASONS.items():
        if not path.endswith(".vue"):
            continue
        source = (ROOT / path).read_text(encoding="utf-8")
        hits = sorted(name for name in SENSITIVE_VIEW_IMPORTS if name in source)
        assert hits == [], f"{path} ordinary but imports {hits}"


def test_security_view_local_components_cannot_be_ordinary() -> None:
    """安全 View 抽到本地组件后，依赖不得因重构落入 ordinary。"""

    pattern = re.compile(SECURITY_VIEW_COMPONENT_IMPORT)
    surfaces = (
        "frontend/src/App.vue",
        *ISSUE_427_FRONTEND_SECURITY_VIEWS,
        *ISSUE_427_CAPABILITY_SECURITY_VIEWS,
    )
    for view_path in surfaces:
        source = (ROOT / view_path).read_text(encoding="utf-8")
        for name in pattern.findall(source):
            component = f"frontend/src/components/{name}"
            assert component not in REVIEWED_ORDINARY_EXACT, component
            category = security_domain_category(component)
            protected = protected_change_category(component)
            assert category == "frontend-security" or protected == "vendor-live"


@pytest.mark.parametrize("path", ISSUE_427_FRONTEND_SECURITY_COMPONENTS)
def test_issue_427_sensitive_components_are_frontend_security(path: str) -> None:
    assert "frontend/src/components/" in FRONTEND_SECURITY_DOMAINS
    assert security_domain_category(path) == "frontend-security"
    result = classify_paths([path])
    assert result.frontend is True
    assert result.security is True
    assert result.full_fallback is False
    assert result.categories == frozenset({"frontend-security"})
    assert classify_changed_paths([path]).risk == "high-risk"


def test_required_source_trees_stay_independent_of_domain_list() -> None:
    assert REQUIRED_TRACKED_SOURCE_TREES == (
        ("backend/app/", (".py",)),
        ("frontend/src/views/", (".vue",)),
        ("frontend/src/components/", (".vue",)),
    )
    assert REQUIRED_TRACKED_ROOT_GLOBS == (
        "frontend/src/*.ts",
        "frontend/src/*.vue",
    )


def test_frontend_root_entry_is_frontend_security_not_ordinary_fallback() -> None:
    assert "frontend/src/*.ts" in FRONTEND_SECURITY_DOMAINS
    assert "frontend/src/*.vue" in FRONTEND_SECURITY_DOMAINS
    assert security_domain_category("frontend/src/main.ts") == "frontend-security"
    assert protected_change_category("frontend/src/main.ts") == "frontend-security"
    result = classify_paths(["frontend/src/main.ts"])
    assert (result.frontend, result.security, result.g2, result.full_fallback) == (
        True,
        True,
        False,
        False,
    )
    assert result.categories == frozenset({"frontend-security"})
    assert classify_changed_paths(["frontend/src/main.ts"]).risk == "high-risk"
    assert "frontend/src/*.ts" in required_codeowners_patterns()
    assert "frontend/src/*.vue" in required_codeowners_patterns()


def test_frontend_lib_is_not_silently_pulled_into_root_security_glob() -> None:
    path = "frontend/src/lib/formatTime.ts"
    assert security_domain_category(path) is None
    result = classify_paths([path])
    assert result.security is False
    assert result.categories == frozenset({"frontend"})


def test_missing_codeowners_pattern_or_gate_would_fail_contract() -> None:
    """合同本身即合并门禁：缺 CODEOWNERS 或缺所需门禁分类会失败。"""

    generated = render_codeowners()
    tampered = "\n".join(
        line
        for line in generated.splitlines()
        if not line.startswith("backend/app/ ")
    ) + "\n"
    assert parse_codeowners_patterns(tampered) != required_codeowners_patterns()
    assert security_domain_category("backend/app/settings.py") == "backend-critical"
    assert classify_paths(["backend/app/settings.py"]).security is True
    assert classify_paths(["backend/app/settings.py"]).g2 is True
