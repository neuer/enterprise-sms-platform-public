from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_release_control.sh"


def test_release_control_smoke_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), "release control smoke script is missing"
    assert os.access(SCRIPT, os.X_OK), "release control smoke script must be executable"


def test_release_control_smoke_builds_current_amd64_candidates_or_uses_four_overrides(
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for token in (
        'API_CANDIDATE_REF="${SMS_RELEASE_CONTROL_API_IMAGE:-}"',
        'WEB_CANDIDATE_REF="${SMS_RELEASE_CONTROL_WEB_IMAGE:-}"',
        'POSTGRES_CANDIDATE_REF="${SMS_RELEASE_CONTROL_POSTGRES_IMAGE:-}"',
        'REDIS_CANDIDATE_REF="${SMS_RELEASE_CONTROL_REDIS_IMAGE:-}"',
        "prepare_candidates()",
        'docker build --platform linux/amd64 -f backend/Dockerfile',
        'docker build --platform linux/amd64 -f frontend/Dockerfile',
        'docker build --platform linux/amd64 -f deploy/postgres.Dockerfile',
        'docker build --platform linux/amd64 -f deploy/redis.Dockerfile',
        'fail "candidate image overrides must be provided together"',
        'API_IMAGE_ID=""',
        'WEB_IMAGE_ID=""',
        'POSTGRES_IMAGE_ID=""',
        'REDIS_IMAGE_ID=""',
        'inspect_candidate "$API_CANDIDATE_REF" API_IMAGE_ID',
        'inspect_candidate "$WEB_CANDIDATE_REF" WEB_IMAGE_ID',
        'inspect_candidate "$POSTGRES_CANDIDATE_REF" POSTGRES_IMAGE_ID',
        'inspect_candidate "$REDIS_CANDIDATE_REF" REDIS_IMAGE_ID',
        'printf -v "$destination"',
        "docker image inspect",
        "linux/amd64",
    ):
        assert token in source

    assert "amd64-ffcecbe" not in source
    assert "sha256:07f1deaea83a50ac7d44d872f0748be" not in source


def test_release_control_smoke_has_explicit_non_gate_language() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for token in (
        "SMS_RELEASE_SMOKE=1",
        "SMS_RELEASE_ROOT=",
        "SMS_RUNTIME_ROOT=",
        "COMPOSE_PROJECT_NAME=",
        "SMS_INGRESS_SUBNET=",
        "git worktree add --detach",
        "secrets.token_hex(8)",
        "tr '[:upper:]' '[:lower:]'",
        "Web-only",
        "API-only",
        "config failure",
        "Web health failure",
        "TERM interruption",
        "failure diagnostics (non-secret)",
        'deploy/sms-compose" release prepare',
        'deploy/sms-compose" release activate',
        'deploy/sms-compose" release status',
        'deploy/sms-compose" release resume',
        "verify_data_images.sh --report",
        "release_control_smoke",
        "purpose=release_control_failure_injection",
        "scan_performed=false",
        "authorized_for_control_smoke=true",
        "control_smoke_only=true",
        "release_scan_performed=false",
        "does not replace the Trivy release gate",
    ):
        assert token in source


def test_release_control_smoke_keeps_sudo_staging_owned_by_the_original_user() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for token in (
        'STAGING_UID="$(id -u)"',
        'if [[ -n "${SUDO_UID:-}" ]]',
        'STAGING_UID="$SUDO_UID"',
        'fail "staging SUDO_UID is invalid"',
        "set_staging_owner()",
        'set_staging_owner "$PARENT/staging" "$bundle"',
        'set_staging_owner "$child"',
    ):
        assert token in source

    assert "unset SUDO_UID" not in source


def test_release_control_smoke_restores_initdb_executable_mode_after_sudo_worktree() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    worktree_add = 'git worktree add --detach "$PLATFORM" "$CANDIDATE_SHA"'
    restore_mode = 'chmod 0755 "$PLATFORM/deploy/initdb/01-create-app-role.sh"'
    prepare = '(cd "$PLATFORM" && bash scripts/local_test.sh prepare)'

    assert restore_mode in source
    assert source.index(worktree_add) < source.index(restore_mode) < source.index(prepare)


def test_release_control_data_image_check_runs_from_inner_platform_worktree() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    invocation = "bash scripts/verify_data_images.sh"
    subshell = '''    (
      cd "$PLATFORM"
      CANDIDATE_SHA='''

    assert subshell in source
    assert source.index(subshell) < source.index(invocation)
    assert source.index(invocation) < source.index("    ) >&2", source.index(invocation))
    assert 'bash "$PLATFORM/scripts/verify_data_images.sh"' not in source


def test_release_control_cleanliness_check_does_not_rewrite_sudo_worktree_index() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'GIT_OPTIONAL_LOCKS=0 git status --porcelain --untracked-files=normal' in source


def test_release_control_smoke_snapshots_default_project_containers_and_volumes() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "docker ps -a" in source
    assert "docker volume ls" in source
    assert "--format" in source
    assert "sms-platform" in source
    assert "cmp -s" in source or "diff -u" in source


def test_release_control_smoke_uses_overridable_isolated_ingress_network() -> None:
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    for token in (
        "SMS_INGRESS_SUBNET",
        "SMS_API_INGRESS_IPV4",
        "SMS_WEB_INGRESS_IPV4",
    ):
        assert token in compose


def test_release_control_smoke_binds_manifests_to_the_observed_migration_head() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for token in (
        'CURRENT_MIGRATION=""',
        "observe_current_migration()",
        "exec -T postgres psql --no-psqlrc --tuples-only --no-align",
        "--username sms_owner --dbname sms",
        "--command 'SELECT version_num FROM alembic_version'",
        'CURRENT_MIGRATION="${BASH_REMATCH[1]}"',
        '"$CURRENT_MIGRATION"',
        '"from": current_migration',
        '"target": current_migration',
    ):
        assert token in source

    assert '"from": "0013_auth_runtime_config"' not in source
