from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DOCKER_PUBLIC_ENVIRONMENT_KEYS = (
    "DOCKER_CONFIG",
    "DOCKER_HOST",
    "SMS_DOCKER_OSXKEYCHAIN_MARKER",
    "SMS_DOCKER_PUBLIC_CONFIG",
    "SMS_DOCKER_PUBLIC_DOCKER_BIN",
    "SMS_DOCKER_PUBLIC_HOST",
    "SMS_DOCKER_PUBLIC_SESSION",
)


def _environment_without_docker_public_session() -> dict[str, str]:
    """Return a subprocess environment outside the enclosing public Docker session."""

    environment = os.environ.copy()
    for name in DOCKER_PUBLIC_ENVIRONMENT_KEYS:
        environment.pop(name, None)
    return environment


def test_spec_consistency_tracks_only_active_contracts() -> None:
    checker = (ROOT / "scripts/check_spec_consistency.py").read_text(encoding="utf-8")

    for active in ("AGENTS.md", "CLAUDE.md", "MAINTENANCE.md", "PRD.md", "PROGRESS.md"):
        assert f'"{active}"' in checker
    assert "全部 38 条" not in checker
    assert "tasks.find(" not in checker
    assert "npm run typecheck && npm run build:g2" in checker
    assert "required_uat_cases.issubset" in checker
    assert "progress_sections" in checker
    assert "PR 与 CI 事实以 GitHub 为准" in checker


def test_local_and_mock_gate_scripts_use_secured_compose_wrapper() -> None:
    scripts = (
        (ROOT / "scripts/local_test.sh").read_text(encoding="utf-8"),
        (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8"),
    )

    for source in scripts:
        assert '"$ROOT/deploy/sms-compose" "$@"' in source
        assert 'SMS_PLATFORM_ROOT="$ROOT"' in source
        assert "SMS_SECRETS_MODE=development" in source
        assert (
            'SMS_RUNTIME_ROOT="${SMS_RUNTIME_ROOT:-${TMPDIR:-/tmp}/'
            'sms-platform-${UID}/secrets}"' in source
        )
        assert "COMPOSE_PROFILES=dev" in source
        assert "docker compose -f deploy/docker-compose.yml" not in source
        assert 'docker compose -f "$COMPOSE_FILE"' not in source


def test_g2_gate_uses_locked_runtimes_and_safe_seed_copy() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")

    assert "uv run pytest" in all_gate
    assert "uv run ruff" in all_gate
    assert "uv run mypy" in all_gate
    assert "node:24-alpine" in all_gate
    assert "node:20-alpine" not in all_gate
    assert "--keys-file /tmp/dev-apikeys.txt" in all_gate
    assert "'exec dd if=\"$1\" status=none'" in all_gate
    assert '> "$temporary"' in all_gate
    assert 'mv "$temporary" "$destination"' in all_gate
    assert 'chmod 600 "$destination"' in all_gate
    assert "cp api:/tmp/dev-apikeys.txt" not in all_gate


def test_g2_unit_tests_enable_debug_with_auth_mock() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")

    assert "../scripts/g2_timing.py" in all_gate
    pytest_line = next(
        line for line in all_gate.splitlines() if "AUTH_MOCK=1 uv run pytest" in line
    )
    assert "DEBUG=1" in pytest_line


def test_g2_migration_check_uses_explicit_test_settings() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    stage_three = all_gate.split("stage_3(){", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    normalized_stage = stage_three.replace("\\\n", " ")

    assert (
        "DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 "
        "uv run python scripts_support/check_migration.py"
    ) in " ".join(normalized_stage.split())


def test_g2_contract_check_uses_explicit_test_settings() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    stage_four = all_gate.split("stage_4(){", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    normalized_stage = stage_four.replace("\\\n", " ")

    assert (
        "DEBUG=1 VENDOR_MOCK=1 AUTH_MOCK=1 "
        "uv run python ../scripts/check_contract.py ../openapi.yaml"
    ) in " ".join(normalized_stage.split())


def test_vendor_live_special_gate_is_mock_only_and_never_uses_network_tools() -> None:
    gate = (ROOT / "scripts/verify_vendor_live_test.sh").read_text(encoding="utf-8")
    postgres_gate = (
        ROOT / "scripts/verify_vendor_postgres_recovery.sh"
    ).read_text(encoding="utf-8")

    assert "VENDOR_MOCK=1" in gate
    assert "AUTH_MOCK=1" in gate
    assert "DEBUG=1" in gate
    assert "check_invariants.py" in gate
    assert "verify_vendor_postgres_recovery.sh" in gate
    assert "test_vendor_uat_recovery_postgres.py" in postgres_gate
    assert "test_export_authorization_postgres.py" in postgres_gate
    assert "test_stable_principal_postgres.py" in postgres_gate
    assert "test_outbox_postgres.py" in postgres_gate
    assert "test_worker_fencing_postgres.py" in postgres_gate
    assert "test_raw_capture_legacy_postgres.py" in postgres_gate
    assert "test_raw_replay_eligibility_postgres.py" in postgres_gate
    assert "test_raw_replay_fencing_postgres.py" in postgres_gate
    assert "test_ops_audit_postgres.py" in postgres_gate
    assert "OUTBOX_POSTGRES_DSN" in postgres_gate
    assert "EXPORT_AUTH_POSTGRES_DSN" in postgres_gate
    assert "alert_channel_availability()" in postgres_gate
    assert "security_daily_resend_api_key" in postgres_gate
    assert "sealed:v1:synthetic-non-secret" in postgres_gate
    assert "https://synthetic.invalid/hook" not in postgres_gate
    assert "SET ROLE sms_callback" in postgres_gate
    assert 'if [[ "${SMS_COVERAGE:-0}" == "1" ]]' in postgres_gate
    assert "pytest_args+=(--cov=app --cov-report= --cov-append)" in postgres_gate
    assert "postgres:16-alpine" in postgres_gate
    assert "POSTGRES_PASSWORD_FILE" in postgres_gate
    assert "DB_OWNER_PASSWORD_FILE" in postgres_gate
    assert "DATA_AES_KEY_FILE" in postgres_gate
    assert "DATA_HMAC_KEY_FILE" in postgres_gate
    assert "secrets.token_bytes(32)" in postgres_gate
    assert 'chmod 700 "$tmp_root"' in postgres_gate
    assert 'chmod 0444 "$owner_password_file"' in postgres_gate
    assert "seq 1 90" in postgres_gate
    assert 'docker inspect --format="{{.State.Status}}"' in postgres_gate
    assert 'docker logs "$container"' in postgres_gate
    assert "test_vendor_test_guard.py" in gate
    assert "test_cli.py" in gate
    assert "test_messages_api.py" in gate
    assert "test_web_messages_api.py" in gate
    assert "test_send_pipeline.py" in gate
    assert "test_send_worker.py" in gate
    assert "test_send_repository.py" in gate
    assert "test_sql_service_repositories.py" in gate
    assert "test_vendor_test_manager.py" in gate
    for test_name in (
        "test_audit_coverage.py",
        "test_auth_runtime.py",
        "test_compose_contract.py",
        "test_prepare_runtime_secrets.py",
        "test_systemd_deployment.py",
        "test_vendor_control_agent.py",
        "test_vendor_control_reload.py",
        "test_vendor_control_client.py",
        "test_vendor_control_journal.py",
        "test_vendor_control_protocol.py",
        "test_vendor_credential_store.py",
        "test_vendor_seal_sessions.py",
        "test_vendor_test_api.py",
        "test_vendor_test_bootstrap.py",
        "test_vendor_test_control_api.py",
        "test_vendor_test_operation.py",
        "test_vendor_test_operation_repository.py",
        "test_vendor_test_recipient.py",
        "test_vendor_test_recipient_repository.py",
        "test_vendor_test_security_audit.py",
        "test_vendor_test_step_up.py",
        "test_vendor_test_uat.py",
        "test_vendor_test_uat_api.py",
        "test_vendor_test_web_console_invariants.py",
        "test_vendor_test_web_console_schema.py",
        "vendor-seal.test.ts",
        "vendor-test-api.test.ts",
        "vendor-test-console.test.ts",
    ):
        assert test_name in gate
    assert "node:24-alpine" in gate
    assert '-v "$ROOT/frontend:/app"' in gate
    assert "-v /app/node_modules" in gate
    assert "G2_NPM_CACHE_DIR:-" in gate
    assert '$G2_NPM_CACHE_DIR:/root/.npm' in gate
    assert "npm_config_cache=/root/.npm" in gate
    assert "npm ci --silent" in gate
    assert "npm test --" in gate
    for command in ("curl ", "wget ", "httpx", "vendor.example.invalid"):
        assert command not in gate
        assert command not in postgres_gate


def test_g2_wraps_exactly_ten_authoritative_stages_with_timing() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")

    calls = re.findall(
        r'^run_stage ([0-9]+) "([^"]+)" stage_([0-9]+)$',
        all_gate,
        re.MULTILINE,
    )
    assert [(int(stage), int(function)) for stage, _name, function in calls] == [
        (stage, stage) for stage in range(10)
    ]
    assert "time.monotonic_ns" in all_gate
    assert "scripts/g2_timing.py record" in all_gate
    assert 'G2_TIMING_FILE:-' in all_gate


def test_g2_records_stage_failure_before_returning_original_status() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    run_stage = all_gate.split("run_stage(){", maxsplit=1)[1].split("\n}", maxsplit=1)[0]

    assert 'status="failure"' in run_stage
    assert run_stage.index("scripts/g2_timing.py record") < run_stage.index(
        'return "$stage_status"'
    )


def test_g2_parent_arms_compose_cleanup_only_before_stack_stage() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")

    assert all_gate.index('run_stage 4 "') < all_gate.index("trap cleanup EXIT")
    assert all_gate.index("trap cleanup EXIT") < all_gate.index('run_stage 5 "')
    stage_five = all_gate.split("stage_5(){", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "trap cleanup EXIT" not in stage_five


def test_g2_uses_optional_buildkit_cache_without_losing_cold_build_path() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    stage_five = all_gate.split("stage_5(){", maxsplit=1)[1].split("\n}", maxsplit=1)[0]

    assert 'if [ -n "${G2_DOCKER_CACHE_DIR:-}" ]' in stage_five
    assert "bash scripts/build_g2_images.sh" in stage_five
    assert "compose up -d\n" in stage_five
    assert "compose up -d --build" in stage_five


def test_g2_frontend_gate_isolates_container_node_modules_from_host() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    frontend_gate = all_gate.split("frontend_gate(){", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    assert 'G2_NPM_CACHE_DIR:-' in frontend_gate
    assert '$G2_NPM_CACHE_DIR:/root/.npm' in frontend_gate
    assert "npm_config_cache=/root/.npm" in frontend_gate
    assert "-v /app/node_modules" in frontend_gate
    for command in (
        "npm ci --silent",
        "npm run build:g2",
        "npm run typecheck",
        "npm test",
    ):
        assert command in frontend_gate


def test_g2_frontend_checks_run_concurrently_after_npm_ci() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    frontend_gate = all_gate.split("frontend_gate(){", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    install_index = frontend_gate.index("npm ci --silent")
    for command in ("npm run build:g2 &", "npm run typecheck &", "npm test &"):
        assert install_index < frontend_gate.index(command)
    assert frontend_gate.count("wait \"$") == 3


def test_g2_bundle_command_avoids_duplicate_typecheck_without_weakening_build() -> None:
    package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]

    assert scripts["build"] == "npm run typecheck && npm run build:g2"
    assert scripts["build:g2"] == "vite build"
    assert "typecheck" not in scripts["build:g2"]


def test_changed_dev_check_does_not_run_deleted_backend_test_paths() -> None:
    source = (ROOT / "scripts/dev_check.sh").read_text(encoding="utf-8")
    changed_test_case = source.split(
        "backend/tests/*.py)",
        maxsplit=1,
    )[1].split(";;", maxsplit=1)[0]

    assert 'if [[ -f "$path" ]]' in changed_test_case
    assert 'backend_tests+=("${path#backend/}")' in changed_test_case


def test_dev_check_avoids_duplicate_frontend_typecheck_and_classifies_shell() -> None:
    source = (ROOT / "scripts/dev_check.sh").read_text(encoding="utf-8")
    frontend = source.split("run_frontend() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    # lint/format 先于组件测试（失败早报错），typecheck 仍只由 build 承载一次
    assert "npm run lint" in frontend
    assert "npm run format:check" in frontend
    assert frontend.index("npm run lint") < frontend.index("npm test")
    assert frontend.index("npm run format:check") < frontend.index("npm test")
    assert "npm test" in frontend
    assert "npm run build" in frontend
    assert "npm run typecheck" not in frontend
    assert "scripts/*.sh" in source
    assert 'bash -n "${shell_scripts[@]}"' in source
    header = source.split("run_contract() {", maxsplit=1)[0]
    assert "scripts/local_test.sh prepare" not in header
    assert "scripts/local_test.sh prepare" in source.split(
        "run_contract() {", maxsplit=1
    )[1]


def test_release_control_terms_the_lifecycle_lock_holder_directly() -> None:
    control = (ROOT / "scripts/verify_release_control.sh").read_text(encoding="utf-8")

    assert "release_activate_interruptibly()" in control
    assert 'exec python3 "$PLATFORM/deploy/scripts/run_with_lifecycle_lock.py"' in control
    assert "--operation release" in control
    assert "release_activate_interruptibly term-resume &" in control
    assert "release_activate term-resume &" not in control


def test_g2_gate_scrapes_required_prometheus_families() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")

    required = (
        "sms_queue_depth",
        "sms_send_rate_per_second",
        "sms_vendor_error_chunks",
        "sms_uncertain_chunks",
        "sms_uncertain_lifecycle_chunks",
        "sms_callback_failures",
        "sms_frequency_filtered_messages",
        "sms_poll_lag_seconds",
        "sms_send_admission",
        "sms_outbox_oldest_age_seconds",
        "sms_send_submit_outcome",
    )
    assert "http://localhost:${api_port}/metrics" in all_gate
    for family in required:
        assert family in all_gate


def test_g2_runs_security_acceptance_after_seed_and_before_uat() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    stage_five = all_gate.split("stage_5(){", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    security = (
        'python3 scripts/security_acceptance.py --base "http://localhost:${web_port}" '
        "--compose-file deploy/docker-compose.yml --secrets-dir deploy/secrets"
    )
    assert security in all_gate
    assert stage_five.rstrip().endswith("seed_dev")
    assert all_gate.index("stage_5(){") < all_gate.index(security)
    assert all_gate.index(security) < all_gate.index(
        "uv run --project backend python scripts/e2e_api.py"
    )


def test_g2_uat_runs_full_default_registry_with_runtime_ports() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    assert all_gate.count("../scripts/e2e_api.py") == 2
    line = next(
        line
        for line in all_gate.splitlines()
        if line.startswith("uv run --project backend python scripts/e2e_api.py")
    )
    assert '--base "http://localhost:${api_port}"' in line
    assert '--mock-base "http://localhost:${mock_vendor_port}"' in line
    assert "--keys deploy/secrets/dev-apikeys.txt" in line
    assert "--compose-file deploy/docker-compose.yml" in line
    assert "--cases" not in line


def test_g2_gate_does_not_execute_performance_loads() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    assert "--include-performance" not in all_gate
    assert "性能冒烟" not in all_gate
    assert not any(
        line.startswith("uv run --project backend python scripts/perf_smoke.py")
        for line in all_gate.splitlines()
    )


def test_release_gate_is_pinned_fail_closed_and_scans_all_release_images() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    assert (
        "aquasec/trivy:0.70.0@sha256:"
        "be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e" in release
    )
    assert "--severity HIGH,CRITICAL" in release
    assert "--exit-code 1" in release
    assert "--scanners vuln" in release
    assert "--no-progress" in release
    for image in (
        "sms-platform-release-api",
        "sms-platform-release-web",
        "sms-platform-release-postgres",
        "sms-platform-release-redis",
    ):
        assert image in release
    assert "docker pull postgres:16" not in release
    assert "docker pull redis:7" not in release


def test_release_gate_records_clean_candidate_and_never_reads_secrets() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    assert "git diff --quiet" in release
    assert "git diff --cached --quiet" in release
    assert 'candidate="$(git rev-parse HEAD)"' in release
    assert "docker build -f backend/Dockerfile" in release
    assert "docker build -f frontend/Dockerfile" in release
    assert "docker build -f deploy/postgres.Dockerfile" in release
    assert "docker build -f deploy/redis.Dockerfile" in release
    assert "trivycache" in release
    assert "deploy/secrets" not in release


def test_release_gate_rebuilds_all_candidates_without_stale_package_cache() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")
    build_commands = [
        line.strip() for line in release.splitlines() if line.strip().startswith("docker build ")
    ]

    assert len(build_commands) == 4
    for command in build_commands:
        assert " --pull " in command
        assert " --no-cache " in command


def test_release_gate_builds_every_candidate_for_the_amd64_server() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")
    build_commands = [
        line.strip() for line in release.splitlines() if line.strip().startswith("docker build ")
    ]

    assert len(build_commands) == 4
    for command in build_commands:
        assert " --platform linux/amd64 " in command
        assert " --provenance=false " in command
        assert " --sbom=false " in command
        assert " --build-arg SOURCE_DATE_EPOCH=0 " in command


def test_release_gate_scans_every_image_before_returning_the_first_failure() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    assert "scan_status=0" in release
    assert "if docker run --rm" in release
    assert 'if [ "$scan_status" -eq 0 ]; then' in release
    assert 'exit "$scan_status"' in release
    assert release.index("docker image inspect") < release.index('exit "$scan_status"')


def test_release_gate_binds_evidence_to_machine_readable_trivy_results() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    assert "mktemp -d" in release
    assert "--format json" in release
    assert 'scan_path="$scan_work_dir/$name.json"' in release
    assert release.index("--format json") < release.index('render_release_evidence.py" release')
    assert "--format cyclonedx" in release
    assert "scripts/canonicalize_sbom.py" in release
    assert "SOURCE_DATE_EPOCH=0" in release
    assert "--sbom api" in release
    assert "--workflow-repository" in release


def test_reproducible_build_helper_is_not_in_the_temporary_release_workflow() -> None:
    rebuild_path = ROOT / "scripts/verify_reproducible_build.sh"
    rebuild = rebuild_path.read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release-gate.yml").read_text(
        encoding="utf-8"
    )

    assert rebuild.count("docker build -f ") == 4
    for option in (
        "--pull",
        "--no-cache",
        "--provenance=false",
        "--sbom=false",
        "--platform linux/amd64",
        "--build-arg SOURCE_DATE_EPOCH=0",
    ):
        assert option in rebuild
    assert "scripts/canonicalize_sbom.py" in rebuild
    assert "scripts/verify_reproducible_release.py" in rebuild
    assert "--format cyclonedx" in rebuild
    assert "--severity HIGH,CRITICAL" not in rebuild
    assert "--scanners vuln" not in rebuild
    assert "bash scripts/verify_reproducible_build.sh" not in workflow
    assert "reproducibility.json" not in workflow
    assert workflow.count("bash scripts/verify_release.sh") == 1
    assert os.access(rebuild_path, os.X_OK)


def test_release_gate_injects_one_version_commit_and_schema_identity() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    assert "scripts/release_metadata.py" in release
    for argument in ("APP_VERSION", "GIT_SHA", "SCHEMA_REVISION"):
        assert release.count(f"--build-arg {argument}=") == 4

    dockerfiles = (
        ROOT / "backend/Dockerfile",
        ROOT / "frontend/Dockerfile",
        ROOT / "deploy/postgres.Dockerfile",
        ROOT / "deploy/redis.Dockerfile",
    )
    for path in dockerfiles:
        source = path.read_text(encoding="utf-8")
        assert 'org.opencontainers.image.version="${APP_VERSION}"' in source
        assert 'org.opencontainers.image.revision="${GIT_SHA}"' in source
        assert 'com.sms-platform.schema-revision="${SCHEMA_REVISION}"' in source
    assert "COPY VERSION ./VERSION" in dockerfiles[0].read_text(encoding="utf-8")
    assert "COPY VERSION /VERSION" in dockerfiles[1].read_text(encoding="utf-8")


def test_release_gate_promotes_same_image_ids_without_repeating_trivy() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    for variable in (
        "RELEASE_API_IMAGE",
        "RELEASE_WEB_IMAGE",
        "RELEASE_POSTGRES_IMAGE",
        "RELEASE_REDIS_IMAGE",
    ):
        assert variable in release
    assert "docker pull --platform linux/amd64" in release
    assert "@sha256:" in release
    assert "RELEASE_SOURCE_REPORT" in release
    assert "--promotion-source" in release
    assert 'render_release_evidence.py" promote' in release
    promoted_branch = release.split('else\n  printf \'\\n复用候选 commit', maxsplit=1)[1]
    assert "docker run --rm" not in promoted_branch


def test_release_gate_does_not_grant_trivy_the_host_docker_socket() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    assert "/var/run/docker.sock" not in release
    assert "docker image save --platform linux/amd64" in release
    assert ':/scan:ro' in release
    assert 'image --input "/scan/$archive_name"' in release


def test_data_image_smoke_uses_file_secrets_and_cleans_ephemeral_state() -> None:
    smoke = (ROOT / "scripts/verify_data_images.sh").read_text(encoding="utf-8")

    for token in (
        "umask 077",
        "mktemp -d",
        "openssl rand",
        "POSTGRES_PASSWORD_FILE=/run/secrets/db_owner_password",
        "deploy/initdb/01-create-app-role.sh",
        "docker volume create",
        "trap cleanup EXIT",
        "docker logs",
    ):
        assert token in smoke
    assert "POSTGRES_PASSWORD=" not in smoke
    assert "deploy/secrets" not in smoke


def test_data_image_smoke_stages_private_secrets_in_container_owned_volume() -> None:
    smoke = (ROOT / "scripts/verify_data_images.sh").read_text(encoding="utf-8")

    for token in (
        "postgres_secret_volume=",
        "--user 0:0",
        "--entrypoint sh",
        "target=/run/secrets",
        "chown postgres:postgres",
        "chmod 0400",
    ):
        assert token in smoke
    assert '"$postgres_secret_volume"' in smoke
    assert "type=bind,source=${tmpdir}/db_owner_password" not in smoke
    assert "type=bind,source=${tmpdir}/db_app_password" not in smoke


def test_data_image_smoke_checks_roles_and_restart_persistence() -> None:
    smoke = (ROOT / "scripts/verify_data_images.sh").read_text(encoding="utf-8")

    for token in (
        "sms_accept",
        "sms_metrics",
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "image_smoke",
        "SET ROLE sms_accept",
        "--appendonly yes",
        "redis-cli SET",
        "redis-cli GET",
        "docker stop",
    ):
        assert token in smoke


def test_redis_domain_smoke_enforces_acl_and_secret_boundaries() -> None:
    smoke = (ROOT / "scripts/verify_redis_domains.sh").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy/redis-domain-entrypoint.sh").read_text(
        encoding="utf-8"
    )

    for token in (
        "domains=(broker auth control)",
        "redis-domain-healthcheck",
        "redis-cli -e ping",
        "--askpass",
        "forbidden_key_for",
        "cross-domain key",
        "credential from another domain",
        "docker restart",
        "AOF restart",
        "KEYS '*'",
        "FLUSHALL",
        ".Config.Env",
        "redis_${domain}_password",
    ):
        assert token in smoke
    assert "REDISCLI_AUTH" not in smoke
    assert "--pass" not in smoke
    for key_rule in (
        "~auth:*",
        "~export:step-up:*",
        "~vendor-test:step-up:*",
        "~quota:*",
        "~freq:*",
        "~idem:*",
        "~queue:paused:*",
        "~realtime",
        "~bulk",
        "~callback",
        "~*.reply.celery.pidbox*",
    ):
        assert key_rule in entrypoint
    assert "scoped Celery pidbox reply key" in smoke
    assert " ~* &*" not in entrypoint
    assert "+@all" not in entrypoint
    assert "user default off" in entrypoint
def test_data_image_smoke_records_versions_from_started_candidate_binaries() -> None:
    smoke = (ROOT / "scripts/verify_data_images.sh").read_text(encoding="utf-8")

    postgres_command = 'docker exec "$postgres_container" postgres --version'
    redis_command = 'docker exec "$redis_container" redis-server --version'
    assert postgres_command in smoke
    assert redis_command in smoke
    assert smoke.index("start_postgres") < smoke.index(postgres_command)
    assert smoke.index("start_redis") < smoke.index(redis_command)
    assert '"$postgres_version_output"' in smoke
    assert '"$redis_version_output"' in smoke
    assert "POSTGRES_VERSION" not in smoke
    assert "REDIS_VERSION" not in smoke


def test_data_image_smoke_waits_for_database_and_initdb_role() -> None:
    smoke = (ROOT / "scripts/verify_data_images.sh").read_text(encoding="utf-8")
    wait_body = smoke.split("wait_postgres() {", 1)[1].split("\n}", 1)[0]

    assert "psql -U sms_owner -d sms_image_smoke -Atc" in wait_body
    assert "FROM pg_roles WHERE rolname = 'sms_accept'" in wait_body
    assert "pg_isready" not in wait_body


def test_release_and_data_gates_accept_only_optional_absolute_report_path() -> None:
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")
    data = (ROOT / "scripts/verify_data_images.sh").read_text(encoding="utf-8")

    assert 'report_path=""' in release
    assert 'sbom_dir=""' in release
    assert 'archive_dir=""' in release
    assert 'scan_dir=""' in release
    assert 'while [ "$#" -gt 0 ]; do' in release
    assert "--report)" in release
    assert "--sbom-dir)" in release
    assert "--archive-dir)" in release
    assert "--scan-dir)" in release
    assert (
        'for absolute_path in "$report_path" "$sbom_dir" "$archive_dir" "$scan_dir"; do'
        in release
    )
    assert 'rm -f -- "$report_path"' in release
    assert "render_release_evidence.py" in release

    assert 'report_path=""' in data
    assert 'if [ "$#" -eq 0 ]; then' in data
    assert 'elif [ "$#" -eq 2 ] && [ "$1" = "--report" ]; then' in data
    assert 'case "$report_path" in' in data
    assert 'rm -f -- "$report_path"' in data
    assert "render_release_evidence.py" in data

    assert release.index('exit "$scan_status"') < release.index(
        'render_release_evidence.py" release'
    )
    assert data.index('if [ "$redis_marker" != "persisted" ]; then') < data.index(
        'render_release_evidence.py" data-images'
    )


def test_g2_runs_release_control_smoke_as_a_non_substitute_control_plane_check() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")

    assert "verify_release_control.sh" in all_gate
    assert "does not replace the Trivy release gate" in all_gate


def test_g2_release_control_reuses_stage_five_amd64_images() -> None:
    all_gate = (ROOT / "scripts/verify_all.sh").read_text(encoding="utf-8")
    stage_nine = all_gate.split("stage_9(){", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    for image in (
        "sms-platform-api:local",
        "sms-platform-web:local",
        "sms-platform-postgres:local",
        "sms-platform-redis:local",
    ):
        assert image in stage_nine
    assert "docker image inspect" in stage_nine
    assert "linux/amd64" in stage_nine
    for variable in (
        "SMS_RELEASE_CONTROL_API_IMAGE",
        "SMS_RELEASE_CONTROL_WEB_IMAGE",
        "SMS_RELEASE_CONTROL_POSTGRES_IMAGE",
        "SMS_RELEASE_CONTROL_REDIS_IMAGE",
    ):
        assert variable in stage_nine


def _write_fake_gate_tools(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    tools = {
        "git": """#!/usr/bin/env bash
case "$*" in
  "rev-parse --show-toplevel") printf '%s\\n' "$REPO_ROOT" ;;
  "rev-parse HEAD") printf '%040d\\n' 0 | tr 0 c ;;
  "diff --quiet"|"diff --cached --quiet"|"status --porcelain --untracked-files=normal") ;;
  *) exit 2 ;;
esac
""",
        "openssl": """#!/usr/bin/env bash
printf '%s\\n' opaque-test-material
""",
        "docker": """#!/usr/bin/env bash
id_value="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
repo_digest="registry.example.com/sms/image@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
bin_dir="$(cd "$(dirname "$0")" && pwd -P)"
if [ "$1" = "context" ] && [ "$2" = "show" ]; then
  printf '%s\\n' fake-local
  exit 0
fi
if [ "$1" = "context" ] && [ "$2" = "inspect" ]; then
  printf '%s\\n' unix:///tmp/fake-docker.sock
  exit 0
fi
if [ "$1" = "info" ] && [ "$2" = "--format" ]; then
  printf '[{"Name":"buildx","Path":"%s/docker-buildx"},' "$bin_dir"
  printf '{"Name":"compose","Path":"%s/docker-compose"}]\\n' "$bin_dir"
  exit 0
fi
if [ "${REQUIRE_PUBLIC_SESSION:-0}" = "1" ] && \
   [ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]; then
  printf '%s\\n' 'fake docker: public session missing' >&2
  exit 92
fi
if [ "$1" = "build" ]; then
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "save" ]; then
  args=("$@")
  for ((index=0; index < ${#args[@]}; index++)); do
    if [ "${args[$index]}" = "--output" ]; then
      printf '%s\n' fake-docker-archive > "${args[$((index + 1))]}"
      exit 0
    fi
  done
  exit 2
fi
if [ "$1" = "run" ]; then
  case "$*" in
    *aquasec/trivy*)
      if [ "${FAIL_SCAN:-0}" = "1" ]; then exit 9; fi
      if [[ "$*" = *"--format cyclonedx"* ]]; then
        printf '%s' '{"bomFormat":"CycloneDX","specVersion":"1.6",'
        printf '%s' '"serialNumber":"urn:uuid:fake","metadata":'
        printf '%s\n' '{"timestamp":"2026-07-28T00:00:00Z"},"components":[]}'
        exit 0
      fi
      image="${@: -1}"
      args=("$@")
      for ((index=0; index < ${#args[@]}; index++)); do
        if [ "${args[$index]}" = "--input" ]; then
          image="${args[$((index + 1))]}"
          break
        fi
      done
      printf '{"SchemaVersion":2,"ArtifactName":"%s",'\
'"ArtifactType":"container_image","Metadata":{"ImageID":"%s"},'\
'"Results":[{"Target":"%s","Class":"os-pkgs",'\
'"Vulnerabilities":[]}]}\n' "$image" "$id_value" "$image"
      exit 0
      ;;
    *) printf '%s\\n' fake-container ; exit 0 ;;
  esac
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  case "$4" in
    "{{.Id}}") printf '%s\\n' "$id_value" ;;
    *RepoDigests*)
      if [ "${EMPTY_REPO_DIGESTS:-0}" = "1" ]; then
        case "$4" in
          *join*)
            printf '%s\\n' 'template parsing error: join expected []string; got []interface {}' >&2
            exit 1
            ;;
          *json*) printf '%s\\n' '[]'; exit 0 ;;
        esac
      fi
      inspected="${@: -1}"
      case "$inspected" in
        *@sha256:*) digest="$inspected" ;;
        *) digest="$repo_digest" ;;
      esac
      case "$4" in
        *json*) printf '["%s"]\\n' "$digest" ;;
        *) printf '%s\\n' "$digest" ;;
      esac
      ;;
    "{{.Os}}/{{.Architecture}}") printf '%s\\n' linux/amd64 ;;
    *)
      printf '%s %s\\n' "$id_value" "$repo_digest"
      printf '%s %s\\n' "$id_value" "$repo_digest"
      printf '%s %s\\n' "$id_value" "$repo_digest"
      printf '%s %s\\n' "$id_value" "$repo_digest"
      ;;
  esac
  exit 0
fi
if [ "$1" = "inspect" ]; then
  exit 1
fi
if [ "$1" = "exec" ]; then
  case "$*" in
    *"postgres --version"*)
      if [ "${BAD_VERSION:-0}" = "1" ]; then
        printf '%s\\n' 'not-postgres'
      else
        printf '%s\\n' 'postgres (PostgreSQL) 16.8'
      fi
      ;;
    *"redis-server --version"*)
      printf '%s\\n' 'Redis server v=7.4.2 sha=00000000:0 malloc=x bits=64 build=abcdef12'
      ;;
    *rolsuper*) printf '%s\\n' '7|true' ;;
    *"FROM pg_roles WHERE rolname = 'sms_accept'"*) printf '%s\\n' 1 ;;
    *"SELECT value FROM image_smoke"*) printf '%s\\n' persisted ;;
    *"redis-cli ping"*) printf '%s\\n' PONG ;;
    *"redis-cli GET"*)
      if [ "${FAIL_PERSIST:-0}" = "1" ]; then
        printf '%s\\n' missing
      else
        printf '%s\\n' persisted
      fi
      ;;
  esac
  exit 0
fi
exit 0
""",
        "docker-buildx": "#!/usr/bin/env sh\nexit 0\n",
        "docker-compose": "#!/usr/bin/env sh\nexit 0\n",
    }
    for name, source in tools.items():
        path = fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    return fake_bin


def test_release_gate_handles_empty_local_repo_digests_from_hosted_docker(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_release.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "EMPTY_REPO_DIGESTS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "template parsing error" not in result.stderr


def test_local_release_candidate_runs_in_public_docker_session(tmp_path: Path) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_release.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "REQUIRE_PUBLIC_SESSION": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_release_gate_retains_one_private_archive_and_raw_scan_per_image(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    evidence = tmp_path / "release-evidence"
    archive_dir = evidence / "images"
    scan_dir = evidence / "scans"
    sbom_dir = evidence / "sboms"
    for directory in (evidence, archive_dir, scan_dir, sbom_dir):
        directory.mkdir()
        directory.chmod(0o700)
    report = evidence / "release-gate.json"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/verify_release.sh"),
            "--report",
            str(report),
            "--archive-dir",
            str(archive_dir),
            "--scan-dir",
            str(scan_dir),
            "--sbom-dir",
            str(sbom_dir),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in archive_dir.iterdir()} == {
        f"{name}.tar" for name in ("api", "web", "postgres", "redis")
    }
    assert {path.name for path in scan_dir.iterdir()} == {
        f"{name}.json" for name in ("api", "web", "postgres", "redis")
    }
    for path in (*archive_dir.iterdir(), *scan_dir.iterdir()):
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    release = (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")
    assert release.count("docker image save --platform linux/amd64") == 1
    assert release.count("--scanners vuln") == 1


def test_release_gate_removes_offline_outputs_when_scanning_fails(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    evidence = tmp_path / "release-evidence"
    archive_dir = evidence / "images"
    scan_dir = evidence / "scans"
    sbom_dir = evidence / "sboms"
    for directory in (evidence, archive_dir, scan_dir, sbom_dir):
        directory.mkdir()
        directory.chmod(0o700)
    report = evidence / "release-gate.json"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/verify_release.sh"),
            "--report",
            str(report),
            "--archive-dir",
            str(archive_dir),
            "--scan-dir",
            str(scan_dir),
            "--sbom-dir",
            str(sbom_dir),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "FAIL_SCAN": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not report.exists()
    assert list(archive_dir.iterdir()) == []
    assert list(scan_dir.iterdir()) == []
    assert list(sbom_dir.iterdir()) == []


def test_registry_promotion_does_not_accept_offline_archive_outputs(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    evidence = tmp_path / "release-evidence"
    archive_dir = evidence / "images"
    scan_dir = evidence / "scans"
    sbom_dir = evidence / "sboms"
    for directory in (evidence, archive_dir, scan_dir, sbom_dir):
        directory.mkdir()
        directory.chmod(0o700)
    environment = _environment_without_docker_public_session()
    environment.update(
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "SMS_DOCKER_ACCESS": "authenticated",
        }
    )
    for name, character in zip(
        ("API", "WEB", "POSTGRES", "REDIS"),
        ("1", "2", "3", "4"),
        strict=True,
    ):
        environment[f"RELEASE_{name}_IMAGE"] = (
            f"registry.example.com/sms/{name.casefold()}@sha256:" + character * 64
        )
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/verify_release.sh"),
            "--report",
            str(evidence / "release-gate.json"),
            "--archive-dir",
            str(archive_dir),
            "--scan-dir",
            str(scan_dir),
            "--sbom-dir",
            str(sbom_dir),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "Registry promotion" in result.stderr


def test_promoted_release_requires_explicit_authenticated_docker_access(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REPO_ROOT": str(ROOT),
    }
    for name, character in zip(
        ("API", "WEB", "POSTGRES", "REDIS"),
        ("1", "2", "3", "4"),
        strict=True,
    ):
        environment[f"RELEASE_{name}_IMAGE"] = (
            f"registry.example.com/sms/{name.casefold()}@sha256:" + character * 64
        )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_release.sh")],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "promoted RepoDigest requires SMS_DOCKER_ACCESS=authenticated" in result.stderr


def test_promoted_release_is_rejected_inside_public_docker_session(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REPO_ROOT": str(ROOT),
        "SMS_DOCKER_ACCESS": "authenticated",
        "SMS_DOCKER_PUBLIC_SESSION": "1",
    }
    for name, character in zip(
        ("API", "WEB", "POSTGRES", "REDIS"),
        ("1", "2", "3", "4"),
        strict=True,
    ):
        environment[f"RELEASE_{name}_IMAGE"] = (
            f"registry.example.com/sms/{name.casefold()}@sha256:" + character * 64
        )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_release.sh")],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "promoted RepoDigest cannot use the public Docker session" in result.stderr


def test_authenticated_docker_access_is_rejected_for_local_candidate(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_release.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "SMS_DOCKER_ACCESS": "authenticated",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "authenticated Docker access requires four promoted RepoDigests" in result.stderr


def test_gate_reports_are_created_only_on_success_and_stale_reports_are_removed(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REPO_ROOT": str(ROOT),
    }
    cases = (
        (ROOT / "scripts/verify_release.sh", "FAIL_SCAN"),
        (ROOT / "scripts/verify_data_images.sh", "FAIL_PERSIST"),
    )

    for script, failure_flag in cases:
        report = tmp_path / f"{script.stem}.json"
        extra_args: list[str] = []
        if script.name == "verify_release.sh":
            sbom_dir = tmp_path / "sboms"
            sbom_dir.mkdir(exist_ok=True)
            extra_args = ["--sbom-dir", str(sbom_dir)]
        success = subprocess.run(
            ["bash", str(script), "--report", str(report), *extra_args],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert success.returncode == 0, success.stderr
        assert json.loads(report.read_text(encoding="utf-8"))["passed"] is True

        report.write_text('{"passed":true,"stale":true}\n', encoding="utf-8")
        failed = subprocess.run(
            ["bash", str(script), "--report", str(report), *extra_args],
            cwd=ROOT,
            env={**environment, failure_flag: "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert not report.exists()


def test_promoted_release_rejects_digest_with_different_candidate_image_id(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    candidate = "c" * 40
    source = tmp_path / "candidate-build-gate.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "gate_type": "release",
                "candidate_commit": candidate,
                "source": {
                    "app_version": "1.6.0",
                    "git_sha": candidate,
                    "schema_revision": "0032_async_import_runtime",
                    "openapi_sha256": "9" * 64,
                    "workflow_repository": "local",
                    "workflow_run_id": 0,
                    "workflow_run_attempt": 0,
                    "sbom_sha256": {
                        name: "8" * 64
                        for name in ("api", "web", "postgres", "redis")
                    },
                },
                "generated_at": "2026-07-15T00:00:00Z",
                "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "d" * 64,
                "images": {
                    name: {
                        "ref": f"sms-platform-release-{name}:{candidate}",
                        "image_id": "sha256:" + "b" * 64,
                        "repo_digests": [],
                        "scan_report_sha256": "e" * 64,
                        "scan_passed": True,
                    }
                    for name in ("api", "web", "postgres", "redis")
                },
                "promotion_source": None,
                "passed": True,
            }
        ),
        encoding="utf-8",
    )
    source.chmod(0o600)
    report = tmp_path / "promoted-release.json"
    environment = _environment_without_docker_public_session()
    environment.update(
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "RELEASE_SOURCE_REPORT": str(source),
            "SMS_DOCKER_ACCESS": "authenticated",
        }
    )
    for name, character in zip(
        ("API", "WEB", "POSTGRES", "REDIS"),
        ("1", "2", "3", "4"),
        strict=True,
    ):
        environment[f"RELEASE_{name}_IMAGE"] = (
            f"registry.example.com/sms/{name.casefold()}@sha256:" + character * 64
        )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_release.sh"), "--report", str(report)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "promotion source image ID" in result.stderr
    assert not report.exists()


def test_data_image_gate_fails_closed_on_nonofficial_version_output(
    tmp_path: Path,
) -> None:
    fake_bin = _write_fake_gate_tools(tmp_path)
    report = tmp_path / "data-images.json"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/verify_data_images.sh"), "--report", str(report)],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPO_ROOT": str(ROOT),
            "BAD_VERSION": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not report.exists()


def test_pytest_warning_policy_is_fail_closed_with_only_exact_upstream_rules() -> None:
    pyproject = tomllib.loads((ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))
    filters = pyproject["tool"]["pytest"]["ini_options"]["filterwarnings"]

    assert filters == [
        "error",
        "ignore:Using `httpx` with `starlette\\.testclient` is deprecated; install "
        "`httpx2` instead\\.\\Z:starlette.exceptions.StarletteDeprecationWarning:"
        "fastapi\\.testclient\\Z",
        "ignore:tagMap is deprecated\\. Please use TAG_MAP instead\\.\\Z:"
        "DeprecationWarning:ldap3\\.utils\\.asn1\\Z",
        "ignore:typeMap is deprecated\\. Please use TYPE_MAP instead\\.\\Z:"
        "DeprecationWarning:ldap3\\.utils\\.asn1\\Z",
    ]
    assert "ignore::DeprecationWarning" not in filters


def test_backend_requires_a_cryptography_release_fixed_for_scanned_cves() -> None:
    pyproject = tomllib.loads((ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert "cryptography>=50.0.0,<51" in dependencies
