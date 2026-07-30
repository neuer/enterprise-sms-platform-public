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


def test_classic_and_qingluan_are_independent_buildable_spas() -> None:
    for path in (
        "frontend/index.html",
        "frontend/src/main.ts",
        "frontend/vite.config.ts",
        "frontend/legacy/index.html",
        "frontend/legacy/src/main.ts",
        "frontend/legacy/vite.config.ts",
        "frontend/legacy/tsconfig.json",
    ):
        assert (ROOT / path).is_file(), path

    qingluan_vite = read("frontend/vite.config.ts")
    qingluan_router = read("frontend/src/router/index.ts")
    classic_vite = read("frontend/legacy/vite.config.ts")
    classic_router = read("frontend/legacy/src/router/index.ts")

    assert 'base: "/next/"' in qingluan_vite
    assert "createWebHistory(import.meta.env.BASE_URL)" in qingluan_router
    assert 'base: "/"' in classic_vite
    assert "createWebHistory()" in classic_router
    assert route_paths(qingluan_router) == route_paths(classic_router)


def test_dual_spas_share_only_the_same_origin_session_contract() -> None:
    qingluan_session = read("frontend/src/stores/session.ts")
    classic_session = read("frontend/legacy/src/stores/session.ts")

    for token in (
        'const TOKEN_KEY = "sms_token"',
        'const USER_KEY = "sms_user"',
        'const CHANGE_TOKEN_KEY = "sms_change_token"',
        'const SESSION_CLEAR_SIGNAL_KEY = "sms_session_clear"',
        "sessionStorage.setItem(TOKEN_KEY, token)",
    ):
        assert token in qingluan_session
        assert token in classic_session

    for source in (qingluan_session, classic_session):
        assert "sessionStorage.setItem(CHANGE_TOKEN_KEY" not in source
        assert "localStorage.setItem(TOKEN_KEY" not in source
        assert "localStorage.setItem(REFRESH_TOKEN_KEY" not in source
        assert "localStorage.setItem(USER_KEY" not in source
        assert "localStorage.setItem(SESSION_CLEAR_SIGNAL_KEY" in source


def test_each_authenticated_shell_exposes_a_same_route_version_switch() -> None:
    qingluan_shell = read("frontend/src/App.vue")
    classic_shell = read("frontend/legacy/src/App.vue")

    assert "返回经典版" in qingluan_shell
    assert "window.location.assign(route.fullPath)" in qingluan_shell
    assert "体验青鸾版" in classic_shell
    assert "window.location.assign(`/next${route.fullPath}`)" in classic_shell


def test_both_spas_cover_authenticated_account_and_ops_workflows() -> None:
    for prefix in ("frontend/src", "frontend/legacy/src"):
        auth_api = read(f"{prefix}/api/auth.ts")
        shell = read(f"{prefix}/App.vue")
        ops_api = read(f"{prefix}/api/ops.ts")
        reports_api = read(f"{prefix}/api/reports.ts")
        ops_view = read(f"{prefix}/views/OpsView.vue")
        report_view = read(f"{prefix}/views/ReportView.vue")

        assert '"/api/v1/web/auth/password/change"' in auth_api
        assert 'data-testid="change-password"' in shell
        assert "DailyPasswordChangeDialog" in shell
        assert "page_size" in ops_api
        assert "alert_type" in ops_api
        assert "processed" in ops_api
        assert "phone" in ops_api
        assert "getExportTask" in ops_view
        assert "downloadExport" in ops_view
        assert "issueExportStepUp" in reports_api
        assert '"X-Export-Step-Up"' in reports_api
        assert "issueExportStepUp" in ops_view
        assert "issueExportStepUp" in report_view
        assert 'inputType: "password"' in ops_view
        assert 'inputType: "password"' in report_view
        assert 'data-testid="download-unmatched-export"' in ops_view


def test_classic_consumes_required_runtime_and_approval_facts() -> None:
    classic_approval_api = read("frontend/legacy/src/api/approvals.ts")
    classic_approval_view = read("frontend/legacy/src/views/ApprovalView.vue")
    classic_dashboard = read("frontend/legacy/src/views/DashboardView.vue")
    classic_send = read("frontend/legacy/src/views/SendView.vue")
    classic_apps = read("frontend/legacy/src/views/AppManagementView.vue")

    for field in (
        "segments",
        "estimated_segments",
        "scheduled_at",
        "trigger_threshold",
        "trigger_threshold_source",
    ):
        assert field in classic_approval_api
        assert field in classic_approval_view

    assert "channel_monitor" in classic_dashboard
    assert "operations" in classic_dashboard
    assert "ui_policy" in classic_send
    assert "ChannelMonitor" in classic_dashboard
    assert "test_send_max" in classic_send
    assert "key_grace_hours" in classic_apps


def test_frontend_package_runs_both_spas_through_existing_gate_commands() -> None:
    package = cast(dict[str, Any], json.loads(read("frontend/package.json")))
    scripts = cast(dict[str, str], package["scripts"])

    for name in (
        "test:qingluan",
        "test:legacy",
        "typecheck:qingluan",
        "typecheck:legacy",
        "build:qingluan",
        "build:legacy",
    ):
        assert name in scripts
    assert "test:qingluan" in scripts["test"] and "test:legacy" in scripts["test"]
    assert "typecheck:qingluan" in scripts["typecheck"]
    assert "typecheck:legacy" in scripts["typecheck"]
    assert "build:qingluan" in scripts["build:g2"]
    assert "build:legacy" in scripts["build:g2"]


def test_single_web_image_contains_classic_root_and_qingluan_next() -> None:
    dockerfile = read("frontend/Dockerfile")
    nginx = read("deploy/nginx.conf")
    compose = cast(
        dict[str, Any],
        yaml.safe_load(read("deploy/docker-compose.yml")),
    )

    assert "COPY --from=build /app/dist/classic /usr/share/nginx/html" in dockerfile
    assert "COPY --from=build /app/dist/next /usr/share/nginx/html/next" in dockerfile
    assert (
        "COPY deploy/nginx-security-headers.conf /etc/nginx/browser-security-headers.conf"
    ) in dockerfile
    assert "location = /next" in nginx
    assert "absolute_redirect off;" in nginx
    assert "return 308 /next/" in nginx
    assert "location ^~ /next/assets/" in nginx
    assert "location /next/" in nginx
    assert "try_files $uri $uri/ /next/index.html" in nginx
    assert nginx.count('add_header Cache-Control "no-cache";') == 2
    assert nginx.count('add_header Cache-Control "public, immutable";') == 2
    assert "location /api/" in nginx

    services = cast(dict[str, Any], compose["services"])
    assert "web" in services
    assert "web-next" not in services
    assert services["web"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -q --spider http://127.0.0.1:8080/ && wget -q --spider http://127.0.0.1:8080/next/",
    ]


def test_dual_frontend_entrypoints_and_rollback_are_documented() -> None:
    frontend_readme = read("frontend/README.md")
    deploy_runbook = read("deploy/README.md")

    for document in (frontend_readme, deploy_runbook):
        assert "经典版：`/`" in document
        assert "青鸾版：`/next/`" in document
        assert "同一个 Web 镜像" in document
    assert "默认入口保持经典版" in frontend_readme
    assert "两套前端回退" in deploy_runbook
    assert "不得只替换 Nginx 配置" in deploy_runbook
