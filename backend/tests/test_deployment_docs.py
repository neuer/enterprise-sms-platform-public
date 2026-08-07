from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
PRODUCTION_SECRETS = {
    "vendor_secret_name",
    "vendor_secret_key",
    "data_aes_key",
    "data_hmac_key",
    "jwt_secret",
    "ldap_bind_password",
    "metrics_scrape_token",
    "db_owner_password",
    "db_auth_password",
    "db_accept_password",
    "db_send_password",
    "db_callback_password",
    "db_export_password",
    "db_scheduler_password",
    "db_metrics_password",
    "redis_broker_password",
    "redis_auth_password",
    "redis_control_password",
}
REDIS_SECRET_NAMES = {
    "redis_broker_password",
    "redis_auth_password",
    "redis_control_password",
}
COMPOSE_INTERNAL_SECRET_ALIASES = {
    "postgres_db_owner_password",
    "postgres_db_auth_password",
    "postgres_db_accept_password",
    "postgres_db_send_password",
    "postgres_db_callback_password",
    "postgres_db_export_password",
    "postgres_db_scheduler_password",
    "postgres_db_metrics_password",
    "redis_broker_server_password",
    "redis_auth_server_password",
    "redis_control_server_password",
    "redis_broker_client_password",
    "redis_auth_client_password",
    "redis_control_client_password",
}
METRIC_FAMILIES = {
    "sms_queue_depth",
    "sms_send_rate_per_second",
    "sms_vendor_error_chunks",
    "sms_uncertain_chunks",
    "sms_callback_failures",
    "sms_frequency_filtered_messages",
    "sms_poll_lag_seconds",
    "sms_usage_projection_drift_dimensions",
    "sms_usage_projection_drift_absolute_delta",
    "sms_worker_stalled_leases",
    "sms_worker_lease_events",
    "sms_metrics_snapshot_age_seconds",
}


def read_required(name: str) -> str:
    path = DEPLOY / name
    assert path.is_file(), f"缺少部署交付物: deploy/{name}"
    return path.read_text(encoding="utf-8")


def test_compose_and_secret_runbook_cover_exact_production_secrets() -> None:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_secrets = compose["secrets"]
    assert set(compose_secrets) == (
        PRODUCTION_SECRETS - REDIS_SECRET_NAMES
    ) | COMPOSE_INTERNAL_SECRET_ALIASES
    assert {Path(item["file"]).name for item in compose_secrets.values()} == PRODUCTION_SECRETS

    runbook = read_required("secrets.md")
    for name in PRODUCTION_SECRETS:
        assert f"`{name}`" in runbook
    assert "0600" in runbook
    assert "test ! -e /run/secrets/db_owner_password" in runbook
    assert "按职责最小挂载" in runbook
    assert "beat | `db_scheduler_password`" in runbook
    assert "DEBUG=0" in runbook
    assert "AUTH_MOCK=0" in runbook
    assert "VENDOR_MOCK=0" in runbook
    assert "不得" in runbook and ".env" in runbook
    assert "db_owner_password" in runbook and "db_metrics_password" in runbook


def test_secret_runbook_documents_canonical_and_runtime_copy_boundaries() -> None:
    runbook = read_required("secrets.md")

    for token in (
        "18 个",
        "0700",
        "0600",
        "/run/sms-platform/secrets/current",
        "0400",
        "current/backend",
        "current/postgres",
        "current/migrate",
        "current/redis",
        "UID 10001",
        "UID 70",
        "UID 999",
        "prepare_runtime_secrets.py",
        "sudo /usr/local/sbin/sms-compose",
        "根 `.env`",
        "--env-file",
    ):
        assert token in runbook
    assert "postgres" in runbook and "migrate" in runbook
    assert "db_owner_password" in runbook and "db_accept_password" in runbook
    redis_runbook = read_required("redis-ha.md")
    for token in ("broker", "auth", "control", "fail closed", "AOF", "replication lag"):
        assert token in redis_runbook


def test_deployment_index_documents_systemd_install_recovery_and_rollback() -> None:
    index = read_required("README.md")

    for token in (
        "/etc/sms-platform/compose.env",
        "install -m 0600",
        "/usr/local/sbin/sms-compose",
        "/etc/systemd/system/sms-platform.service",
        "systemd-analyze verify",
        "systemctl daemon-reload",
        "systemctl enable --now sms-platform.service",
        "systemctl status sms-platform.service",
        "systemctl disable --now sms-platform.service",
        "systemctl restart sms-platform.service",
        "systemctl restart docker.service",
        "删除 `/run/sms-platform/secrets`",
        "reboot",
        "回退",
    ):
        assert token in index
    assert "sudo /usr/local/sbin/sms-compose up -d --remove-orphans" in index
    assert "明文 HTTP 上游默认绑定回环" in index
    assert "公开入口只允许经审批的 `18443`" in index
    assert "80/443/8080/8443" in index


def test_runtime_secret_docs_define_production_up_allowlist_and_shared_lifecycle_lock() -> None:
    index = read_required("README.md")
    secrets = read_required("secrets.md")

    for token in (
        "production `up` 白名单",
        "mock-vendor",
        "--scale",
        "--force-recreate",
        "postgres",
        "worker-callback",
        "共享",
        "lifecycle flock",
        "up/down/run --rm migrate/rotate backend",
    ):
        assert token in index
    assert "共享" in secrets
    assert "lifecycle flock" in secrets
    assert "up/down/run --rm migrate/rotate backend" in secrets


def test_runtime_secret_docs_cover_portable_lock_and_reboot_bootstrap() -> None:
    index = read_required("README.md")
    secrets = read_required("secrets.md")

    for token in (
        "run_with_lifecycle_lock.py",
        "Python `fcntl.flock`",
        "首次启动/reboot",
        "0700",
        "绝对路径",
        "尾斜杠",
        "符号链接",
    ):
        assert token in index
    assert "run_with_lifecycle_lock.py" in secrets
    assert "Python `fcntl.flock`" in secrets
    assert "首次启动/reboot" in secrets
    for token in (
        "verify-held",
        "pass_fds",
        "继承锁 FD",
        "传入 FD 本身",
        "TERM/INT/HUP",
        "SIGKILL",
    ):
        assert token in index
        assert token in secrets


def test_unified_four_image_release_runbooks_close_operator_contract() -> None:
    index = read_required("README.md")
    secrets = read_required("secrets.md")
    handover = (ROOT / "HANDOVER.md").read_text(encoding="utf-8")

    for token in (
        "## 四镜像统一发布",
        "manifest.json",
        "render_release_evidence.py",
        "create_release_manifest.py",
        "gate_type=release",
        "gate_type=release_control_smoke",
        "不替代 Trivy",
        "development archive",
        "production preloaded digest",
        "release prepare --manifest",
        "release activate --release-id",
        "release status --release-id",
        "release resume --release-id",
        "release rollback --release-id",
        "维护窗口",
        "quiesce_backend",
        "wait_beat_lease",
        "31 秒",
        "recreate_postgres",
        "recreate_redis",
        "run_migrate",
        "recreate_backend",
        "recreate_web",
        "final_runtime",
        "residual_changes",
        "recovery_required",
        "跨大版本",
        "破坏性迁移",
        "首次引导",
        "不自动 prune",
        "deploy_release_remote.py",
        "工具沙箱",
    ):
        assert token in index

    order = (
        "quiesce_backend",
        "wait_beat_lease",
        "recreate_postgres",
        "recreate_redis",
        "run_migrate",
        "recreate_backend",
        "recreate_web",
        "final_runtime",
    )
    positions = [index.index(token, index.index("## 四镜像统一发布")) for token in order]
    assert positions == sorted(positions)

    for token in (
        "release prepare",
        "release activate/resume/rollback",
        "同一个 lifecycle flock",
        "运行密钥",
        "不打印值、长度、摘要或哈希",
    ):
        assert token in secrets

    for token in (
        "远端 Mock 发布演练",
        "生产变更单",
        "release_control_smoke",
        "不代表发布就绪",
        "保留发布包、旧镜像和事件记录",
    ):
        assert token in handover


def test_production_release_reuses_candidate_scan_for_same_promoted_image_ids() -> None:
    index = read_required("README.md")

    for token in (
        "RELEASE_API_IMAGE",
        "RELEASE_WEB_IMAGE",
        "RELEASE_POSTGRES_IMAGE",
        "RELEASE_REDIS_IMAGE",
        "RELEASE_SOURCE_REPORT",
        "最终 RepoDigest",
        "scan_report_sha256",
        "image ID 与候选逐一相等",
        "相同内容不重复扫描",
        "create_release_manifest.py",
    ):
        assert token in index
    assert "再用 `render_release_evidence.py release` 生成" not in index
    assert "python3 scripts/render_release_evidence.py release" not in index


def test_remote_release_runbook_requires_mode_git_systemd_and_browser_acceptance() -> None:
    index = read_required("README.md")

    for token in (
        "uv run --project backend python scripts/deploy_release_remote.py",
        "SMS_SECRETS_MODE",
        "systemctl is-active",
        "git status --porcelain",
        "浏览器登录验收",
    ):
        assert token in index


def test_development_high_risk_release_reload_boundary_is_documented() -> None:
    index = read_required("README.md")
    controlled = (ROOT / "docs/runbooks/controlled-real-vendor-test.md").read_text(encoding="utf-8")
    reset_plan_path = ROOT / "docs/plans/2026-07-19-vendor-test-reset-and-step-up.md"
    if not reset_plan_path.is_file():
        pytest.skip("private-only reset plan is intentionally absent from public snapshot")
    reset_plan = reset_plan_path.read_text(encoding="utf-8")

    for document in (index, controlled, reset_plan):
        for token in (
            "vendor-control-agent.service",
            "release-rollback/",
            "/usr/bin/systemctl restart",
            "systemctl is-active --quiet",
            "succeeded",
        ):
            assert token in document
        assert "失败关闭" in document or "fail-closed" in document


def test_dba_runbook_rotates_database_passwords_without_plaintext_arguments() -> None:
    runbook = read_required("dba.md")
    rotation = runbook.split("## 数据库密码轮换", 1)[1].split("\n## ", 1)[0]

    for token in (
        "七个运行密码",
        "db-role-provision",
        "角色属性",
        "secret 文件",
        "sudo /usr/local/sbin/sms-compose",
        "db_accept_password",
        "db_owner_password",
    ):
        assert token in runbook
    assert rotation.index("1.") < rotation.index("2.") < rotation.index("3.")
    assert "--password" not in runbook


def test_production_runbooks_never_use_raw_compose_entrypoint() -> None:
    for name in (
        "secrets.md",
        "README.md",
        "dba.md",
        "backup-restore.md",
        "vendor-egress.md",
    ):
        runbook = read_required(name)
        assert "docker compose -f deploy/docker-compose.yml" not in runbook
    handover = (ROOT / "HANDOVER.md").read_text(encoding="utf-8")
    assert "docker compose -f deploy/docker-compose.yml" not in handover
    assert "sudo /usr/local/sbin/sms-compose" in handover


def test_backup_runbook_streams_encryption_and_verifies_isolated_restore() -> None:
    runbook = read_required("backup-restore.md")
    required = (
        "pg_dump",
        "--format=custom",
        "| openssl enc",
        "-aes-256-cbc",
        "-pbkdf2",
        "sha256",
        "chmod 600",
        "pg_restore",
        "sms_restore",
        "alembic_version",
        "audit_log",
        "UPDATE",
        "DELETE",
    )
    for token in required:
        assert token in runbook
    assert "明文 dump" in runbook and "禁止" in runbook
    assert "BACKUP_PASSPHRASE_FILE" in runbook
    assert "--password" not in runbook
    for line in runbook.splitlines():
        if "pg_dump" in line and not line.lstrip().startswith("#"):
            assert not re.search(r"pg_dump[^|]*>\s*[^|]+\.dump\b", line)


def test_vendor_runbook_requires_primary_and_standby_written_confirmation() -> None:
    runbook = read_required("vendor-egress.md")
    for token in (
        "主出口 IP",
        "备出口 IP",
        "同一",
        "书面",
        "QPS",
        "单次号码上限",
        "1010",
        "crit",
        "SecretKey",
        "GetBalance",
    ):
        assert token in runbook
    assert "禁止" in runbook and "工单" in runbook


def test_failover_runbook_closes_daily_sync_and_manual_cutover_safety() -> None:
    runbook = read_required("failover.md")
    for token in (
        "sync_standby.py",
        "restore_drill.py",
        "每日",
        "RPO≤24h",
        "systemd",
        "cron",
        "secrets",
        "双人复核",
        "双 beat",
        "双拉取",
        "DNS",
        "/etc/hosts",
        "主出口 IP",
        "备出口 IP",
        "postgres",
        "redis",
        "api",
        "worker-realtime",
        "worker-bulk",
        "worker-callback",
        "outbox-dispatcher",
        "beat",
        "web",
        "raw_vendor_log",
        "unmatched",
        "uncertain",
        "回切",
        "[HANDOVER]",
        "RTO≤30min",
    ):
        assert token in runbook
    assert "不启动" in runbook and "不传输" in runbook
    assert "禁止自动重发" in runbook


def test_prometheus_example_is_internal_and_covers_metric_checklist() -> None:
    config_path = DEPLOY / "prometheus.example.yml"
    assert config_path.is_file(), "缺少 deploy/prometheus.example.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    job = config["scrape_configs"][0]
    assert job["metrics_path"] == "/metrics"
    assert job["scrape_interval"] == "15s"
    assert job["scrape_timeout"] == "3s"
    assert job["authorization"]["credentials_file"] == (
        "/run/secrets/metrics_scrape_token"
    )
    assert job["static_configs"][0]["targets"] == ["api:8000"]

    index = read_required("README.md")
    assert "监控网段" in index and "公网" in index
    assert "METRICS_ALLOWED_CIDRS" in index
    assert "开发测试阶段不执行生产监控/故障切换验收" in index
    assert "up == 0" in index
    for family in METRIC_FAMILIES:
        assert family in index


def test_deployment_index_links_all_authoritative_runbooks() -> None:
    index = read_required("README.md")
    for target in (
        "secrets.md",
        "dba.md",
        "backup-restore.md",
        "vendor-egress.md",
        "prometheus.example.yml",
        "failover.md",
    ):
        assert target in index
    assert "PRD.md 第 10 章" in index
    assert "HANDOVER.md 第 1 节" in index


def test_deployment_index_documents_the_independent_release_gate() -> None:
    index = read_required("README.md")
    handover = (ROOT / "HANDOVER.md").read_text(encoding="utf-8")

    for token in (
        "verify_release.sh",
        "Trivy 0.70.0",
        "HIGH/CRITICAL",
        "trivycache",
        "镜像 digest",
        "独立于 G2",
    ):
        assert token in index
    assert "扫描报告" in handover
    assert "最终镜像 digest" in handover


def test_remote_mac_docs_separate_public_docker_from_authenticated_registry() -> None:
    index = read_required("README.md")
    fast_update = (ROOT / "docs/runbooks/test-fast-update.md").read_text(encoding="utf-8")
    combined = "\n".join((index, fast_update))

    for token in (
        "手机远程 Mac",
        "scripts/docker_public.sh doctor",
        "macOS Keychain",
        "SMS_DOCKER_ACCESS=authenticated",
        "受控 CI",
        "私有仓库",
        "不得删除",
        "不得解锁",
    ):
        assert token in combined
    assert "scripts/docker_public.sh doctor" in fast_update
    assert "SMS_DOCKER_ACCESS=authenticated" in index
    for forbidden_destination in ("聊天", "命令参数", "日志", "发布证据", "仓库"):
        assert forbidden_destination in index


def test_release_docs_use_four_final_alpine_images_and_persistence_smoke() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    index = read_required("README.md")
    env_example = read_required(".env.example")

    assert "python:3.12-alpine" in agents
    assert "node:24-alpine" in agents
    assert "python:3.12-slim / node:20" not in agents
    for token in (
        "SMS_API_IMAGE",
        "SMS_WEB_IMAGE",
        "SMS_POSTGRES_IMAGE",
        "SMS_REDIS_IMAGE",
    ):
        assert token in index
        assert token in env_example
    assert "verify_data_images.sh" in index
    assert "四个最终交付镜像" in index
    assert "0 HIGH / 0 CRITICAL" in index


def test_account_provider_deployment_is_local_first_and_not_environment_configured() -> None:
    index = read_required("README.md")
    env_example = read_required(".env.example")

    for token in (
        "sms-compose init-admin --show-temporary-password",
        "只允许空系统",
        "Codex 可通过 PTY 执行",
        "与 AD 配置、LDAP bind 和目录可用性无关",
        "系统配置页",
        "保存草稿",
        "测试连接",
        "启用配置",
        "禁用 AD",
        "配置与角色映射",
    ):
        assert token in index

    assert "LDAP_BIND_PASSWORD_FILE=/run/secrets/ldap_bind_password" in env_example
    assert "LDAP_CA_CERTS_FILE=" in env_example
    for retired in (
        "BOOTSTRAP_ADMIN_USERS",
        "LDAP_SERVER",
        "LDAP_BASE_DN",
        "LDAP_BIND_DN",
        "LDAP_USER_SEARCH_FILTER",
        "LDAP_CONNECT_TIMEOUT_S",
        "LDAP_RECEIVE_TIMEOUT_S",
    ):
        assert retired not in env_example
        assert retired not in index


def test_public_handover_redacts_evidence_and_acceptance_stays_live() -> None:
    acceptance = (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8")
    progress = (ROOT / "PROGRESS.md").read_text(encoding="utf-8")
    handover = (ROOT / "HANDOVER.md").read_text(encoding="utf-8")

    assert "<redacted-" in handover
    assert re.search(r"\b[0-9a-f]{40}\b", handover) is None
    for token in ("API 0", "Web 0", "PostgreSQL 0", "Redis 0"):
        assert token in handover
    assert "<redacted-" not in acceptance
    assert "## 2026-" not in acceptance
    assert "CI 事实以 GitHub 为准" in acceptance
    assert "生产候选" in acceptance and "release manifest" in acceptance
    assert "活跃外部阻塞" in progress
    assert re.search(r"\b[0-9a-f]{40}\b", progress) is None
    for token in (
        "外部 TLS",
        "真实 AD",
        "生产 18 件 secrets",
        "24 小时",
        "RTO",
        "真人 UAT",
    ):
        assert token in handover


def test_bookkeeping_moves_final_head_evidence_to_change_order() -> None:
    documents = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in ("PROGRESS.md", "HANDOVER.md")
    }
    stale_phrases = (
        "本次文档合并后的新 HEAD 仍须",
        "本次文档合并后对新 main HEAD 重跑",
        "本次文档合并后对新 `main` HEAD 重跑",
    )

    for name, document in documents.items():
        for phrase in stale_phrases:
            assert phrase not in document, f"{name} 仍包含已完成门禁的旧续跑提示"
    assert (
        "最终不可变证据归档到生产变更单与 release manifest"
        in documents["HANDOVER.md"]
    )
    assert "`MAINTENANCE.md` 为准" in documents["PROGRESS.md"]


def test_deployment_index_assigns_hsts_to_the_external_tls_terminator() -> None:
    index = read_required("README.md")
    handover = (ROOT / "HANDOVER.md").read_text(encoding="utf-8")

    for token in (
        "Strict-Transport-Security",
        "max-age>=31536000",
        "includeSubDomains",
        "WEB_BASE_URL",
        "HTTPS 重定向",
        "CSP console",
    ):
        assert token in index
    assert "内部 HTTP Nginx" in index
    assert "外部 TLS 终结器" in handover


def test_test_secure_access_runbooks_define_the_phone_operator_boundary() -> None:
    index = read_required("README.md")
    egress = read_required("vendor-egress.md")
    controlled = (ROOT / "docs/runbooks/controlled-real-vendor-test.md").read_text(
        encoding="utf-8"
    )
    manual = (ROOT / "docs/TEST-MANUAL.md").read_text(encoding="utf-8")
    combined = "\n".join((index, egress, controlled, manual))

    for token in (
        "打开正式凭据安全入口",
        "sms-compose secure-access start",
        "sms-compose secure-access status",
        "sms-compose secure-access stop",
        "15 分钟",
        "trycloudflare.com",
        "Cloudflare Quick Tunnel",
        "重新登录",
        "isSecureContext=true",
        "crypto.subtle",
        "HTTP 入口",
        "隐藏",
        "不得降级",
        "仅限开发测试",
        "无 SLA",
        "服务器不自行下载",
        "2026.7.2",
        "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd",
        "不自动启动",
        "正式 Key",
        "测试号码",
        "激活",
        "管理员初始化",
        "PostgreSQL",
        "Docker volume",
        "旧 URL",
        "cloudflared 进程",
    ):
        assert token in combined

    for document in (index, controlled):
        assert "打开正式凭据安全入口" in document
        assert "sms-compose secure-access start" in document
        assert "sms-compose secure-access stop" in document
