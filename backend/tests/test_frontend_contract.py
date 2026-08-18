from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def route_paths(source: str) -> set[str]:
    return set(re.findall(r'path:\s*"(/[^"]*)"', source))


def test_qingluan_is_the_only_root_spa() -> None:
    for path in (
        "frontend/index.html",
        "frontend/src/main.ts",
        "frontend/vite.config.ts",
        "frontend/tsconfig.json",
    ):
        assert (ROOT / path).is_file(), path

    assert not (ROOT / "frontend/legacy").exists()
    vite = read("frontend/vite.config.ts")
    router = read("frontend/src/router/index.ts")

    assert 'base: "/"' in vite
    assert "createWebHistory(import.meta.env.BASE_URL)" in router
    assert route_paths(router) == {
        "/",
        "/login",
        "/change-password",
        "/dashboard",
        "/reports",
        "/users",
        "/configs",
        "/audit",
        "/send",
        "/approvals",
        "/replies",
        "/batches",
        "/messages",
        "/callbacks",
        "/ops",
        "/security-daily",
        "/templates",
        "/signs",
        "/apps",
        "/blacklist",
        "/sensitive-words",
        "/:pathMatch(.*)*",
    }
    assert '{ path: "/:pathMatch(.*)*", redirect: "/dashboard" }' in router


def test_single_spa_keeps_the_browser_session_contract() -> None:
    session = read("frontend/src/stores/session.ts")

    for token in (
        'const CHANGE_TOKEN_KEY = "sms_change_token"',
        'const SESSION_CLEAR_SIGNAL_KEY = "sms_session_clear"',
    ):
        assert token in session

    assert "sessionStorage.setItem(TOKEN_KEY" not in session
    assert "sessionStorage.setItem(USER_KEY" not in session
    assert "sessionStorage.setItem(CHANGE_TOKEN_KEY" not in session
    assert "localStorage.setItem(TOKEN_KEY" not in session
    assert "localStorage.setItem(REFRESH_TOKEN_KEY" not in session
    assert "localStorage.setItem(USER_KEY" not in session
    assert "localStorage.setItem(SESSION_CLEAR_SIGNAL_KEY" in session

    session_tokens = read("frontend/src/api/sessionTokens.ts")
    assert "setAccessSession" in session_tokens
    assert "clearAccessSession" in session_tokens


def test_authenticated_shell_has_no_frontend_version_switch() -> None:
    shell = read("frontend/src/App.vue")

    assert "返回经典版" not in shell
    assert "体验青鸾版" not in shell
    assert "goToClassic" not in shell
    assert "goToQingluan" not in shell
    assert 'data-testid="change-password"' in shell
    assert "DailyPasswordChangeDialog" in shell


def test_single_spa_covers_account_ops_and_vendor_security_workflows() -> None:
    auth_api = read("frontend/src/api/auth.ts")
    admin_api = read("frontend/src/api/admin.ts")
    callback_api = read("frontend/src/api/callbacks.ts")
    ops_api = read("frontend/src/api/ops.ts")
    reports_api = read("frontend/src/api/reports.ts")
    ops_view = read("frontend/src/views/OpsView.vue")
    report_view = read("frontend/src/views/ReportView.vue")
    credential_dialog = read("frontend/src/components/VendorCredentialDialog.vue")
    vendor_seal = read("frontend/src/lib/vendorSeal.ts")

    assert '"/api/v1/web/auth/password/change"' in auth_api
    for field in ("page_size", "alert_type", "processed", "phone"):
        assert field in ops_api
    assert "getExportTask" in ops_view
    assert "downloadExport" in ops_view
    assert "issueExportStepUp" in reports_api
    assert '"X-Export-Step-Up"' in reports_api
    assert "issueExportStepUp" in ops_view
    assert "issueExportStepUp" in report_view
    assert 'inputType: "password"' in ops_view
    assert 'inputType: "password"' in report_view
    assert 'data-testid="download-unmatched-export"' in ops_view
    assert "reset_configuration" in admin_api
    assert "correlation_id" in admin_api
    assert "correlation_id" in callback_api
    assert "isVendorCredentialSecureContext" in credential_dialog
    assert "isVendorCredentialSecureContext" in vendor_seal


def test_single_spa_consumes_required_runtime_and_approval_facts() -> None:
    approval_api = read("frontend/src/api/approvals.ts")
    approval_view = read("frontend/src/views/ApprovalView.vue")
    dashboard = read("frontend/src/views/DashboardView.vue")
    send = read("frontend/src/views/SendView.vue")
    apps = read("frontend/src/views/AppManagementView.vue")

    for field in (
        "segments",
        "estimated_segments",
        "scheduled_at",
        "trigger_threshold",
        "trigger_threshold_source",
    ):
        assert field in approval_api
        assert field in approval_view

    assert "channel_monitor" in dashboard
    assert "operations" in dashboard
    assert "ui_policy" in send
    assert "ChannelMonitor" in dashboard
    assert "test_send_max" in send
    assert "key_grace_hours" in apps


def test_frontend_package_exposes_one_canonical_gate_set() -> None:
    package = cast(dict[str, Any], json.loads(read("frontend/package.json")))
    scripts = cast(dict[str, str], package["scripts"])

    assert scripts == {
        "dev": "vite --host 0.0.0.0",
        "build": "npm run typecheck && npm run build:g2",
        "build:g2": "vite build",
        "typecheck": "vue-tsc --noEmit",
        "test": "vitest run",
    }
    assert all("legacy" not in name and "qingluan" not in name for name in scripts)


def test_single_web_image_serves_qingluan_at_root() -> None:
    dockerfile = read("frontend/Dockerfile")
    nginx = read("deploy/nginx.conf")
    compose = cast(
        dict[str, Any],
        yaml.safe_load(read("deploy/docker-compose.yml")),
    )

    assert "COPY --from=build /app/dist /usr/share/nginx/html" in dockerfile
    assert "/app/dist/classic" not in dockerfile
    assert "/app/dist/next" not in dockerfile
    assert (
        "COPY deploy/nginx-security-headers.conf /etc/nginx/browser-security-headers.conf"
    ) in dockerfile
    assert "location ~ ^/next(?:/|$)" in nginx
    assert "return 410;" in nginx
    assert "try_files $uri $uri/ /next/index.html" not in nginx
    assert nginx.count('add_header Cache-Control "no-cache";') == 1
    assert nginx.count('add_header Cache-Control "public, immutable";') == 1
    assert "location /api/" in nginx
    assert "try_files $uri $uri/ /index.html" in nginx

    services = cast(dict[str, Any], compose["services"])
    assert "web" in services
    assert services["web"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -q --spider http://127.0.0.1:8080/",
    ]


def test_single_frontend_entrypoint_and_rollback_are_documented() -> None:
    root_readme = read("README.md")
    frontend_readme = read("frontend/README.md")
    deploy_runbook = read("deploy/README.md")

    assert "双前端" not in root_readme
    assert "青鸾单一前端" in frontend_readme
    assert "唯一入口：`/`" in frontend_readme
    assert "单前端入口与回退" in deploy_runbook
    assert "`/next`" in deploy_runbook and "`410 Gone`" in deploy_runbook
    assert "整个上一版 Web 镜像" in deploy_runbook
