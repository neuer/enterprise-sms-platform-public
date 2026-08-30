from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "scripts/test_update.sh"
CONTRACT = ROOT / "deploy/scripts/test_update_contract.py"
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from test_update_contract import HOST_CONTROL_PATHS  # noqa: E402


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def test_driver_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(DRIVER)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_driver_rejects_noncanonical_remote_root_before_git_or_network() -> None:
    result = subprocess.run(
        [
            "bash",
            str(DRIVER),
            "--dry-run",
            "--ref",
            "origin/main",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_ROOT": "/home/smsdeploy/enterprise-sms-platform",
            "SMS_TEST_UPDATE_PORT": "22",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "test-update: SMS_TEST_UPDATE_ROOT 必须为 /opt/sms-platform"
    )


def test_driver_accepts_no_secret_or_phone_arguments_and_uses_fixed_remote_commands() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    for forbidden in (
        "--secret",
        "secret_name=",
        "secret_key=",
        "phone=",
        "mobile=",
    ):
        assert forbidden not in source.lower()
    assert "sms-compose test-update prepare" in source
    assert "sms-compose test-update apply" in source
    assert "sms-compose test-update verify" in source
    assert "sms-compose test-update status" in source
    assert "remote_git_preflight" in source
    assert "远端 Git 基线不可由更新用户读取" in source
    assert "eval " not in source


def test_driver_requires_exact_ci_for_high_risk_and_keeps_host_control_binding() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    high_risk_control = source.split(
        'if [[ "$RISK" == high-risk &&',
        maxsplit=1,
    )[1].split(
        "fi",
        maxsplit=1,
    )[0]

    assert "deploy/scripts/test_update_contract.py" in source
    assert "classify-nul" in source
    assert 'if [[ "$RISK" == high-risk || "$MIGRATION_CHANGED" == 1 ]]' in source
    assert "scripts/verify_ci_commit.py" in source
    assert "--commit \"$TARGET_COMMIT\"" in source
    assert "api.github.com" not in source
    assert "scripts/verify_all.sh" not in source
    assert "scripts/verify_vendor_live_test.sh" not in source
    assert (
        'BOOTSTRAP_SMS_COMPOSE="/usr/local/libexec/sms-platform/'
        'test-secure-access/sms-compose-bootstrap"'
    ) in source
    assert 'REMOTE_SMS_COMPOSE="$BOOTSTRAP_SMS_COMPOSE"' in source
    assert high_risk_control.index(
        'REMOTE_SMS_COMPOSE="$BOOTSTRAP_SMS_COMPOSE"'
    ) < high_risk_control.index("test-update capability")
    assert "test-update capability" in source
    assert 'value["host_control_snapshot"] is True' in source
    assert '"source_commit"' in source
    assert "HOST_CONTROL_PATHS=(" in source
    assert 'git -C "$ROOT" diff --quiet' in source
    assert '"$HOST_SOURCE_COMMIT" "$TARGET_COMMIT"' in source
    assert 'if [[ "$HOST_CONTROL_CHANGED" == 1 &&' in source
    assert '"$HOST_SOURCE_COMMIT" != "$TARGET_COMMIT"' in source
    assert "host-control" in source
    assert 'case "$path"' not in source
    assert "export VENDOR_MOCK=1" not in source
    assert "sms-compose VENDOR_MOCK" not in source
    for forbidden in (
        "down -v",
        "volume rm",
        "pg_restore",
        "alembic downgrade",
        "flushall",
        "seed-dev",
        "reset --hard",
    ):
        assert forbidden not in source.lower()


def test_driver_and_classifier_share_the_exact_host_control_asset_set() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    match = re.search(r"HOST_CONTROL_PATHS=\(\n(?P<body>.*?)\n\)", source, re.DOTALL)

    assert match is not None
    driver_paths = frozenset(
        re.findall(r'^\s+"([^"]+)"$', match.group("body"), re.MULTILINE)
    )
    assert driver_paths == HOST_CONTROL_PATHS


def test_high_risk_remote_phases_stay_on_one_immutable_host_wrapper() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert source.count('REMOTE_SMS_COMPOSE="$BOOTSTRAP_SMS_COMPOSE"') == 1
    assert 'REMOTE_SMS_COMPOSE="/usr/local/sbin/sms-compose"' in source
    apply_offset = source.index(
        "# 固定远端阶段: sms-compose test-update apply"
    )
    verify_offset = source.index(
        "# 固定远端阶段: sms-compose test-update verify"
    )
    status_offset = source.index(
        "# 固定远端阶段: sms-compose test-update status"
    )
    assert 'REMOTE_SMS_COMPOSE=' not in source[apply_offset:]
    assert apply_offset < verify_offset < status_offset
    assert 'FINAL_STATUS="$(remote_sms_compose test-update status)"' in source
    assert "verify-status" in source
    assert '"$UPDATE_ID" "$TARGET_COMMIT" "$MIGRATION_TARGET"' in source


def test_driver_uses_component_specific_services_and_immutable_image_ids() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "web-only" in source
    assert "backend-safe" in source
    assert "docker image inspect" in source
    assert "sha256:" in source
    assert "mock-vendor" not in source


def test_driver_binds_both_images_to_target_commit_and_schema_labels() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert 'APP_VERSION="$(tr -d \'\\n\' <"$WORKTREE/VERSION")"' in source
    assert source.count('--build-arg "APP_VERSION=$APP_VERSION"') == 2
    assert source.count('--build-arg "GIT_SHA=$TARGET_COMMIT"') == 2
    assert source.count('--build-arg "SCHEMA_REVISION=$MIGRATION_TARGET"') == 2


def test_driver_does_not_repeat_backend_or_frontend_ci_commands() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "run_frontend_gates" not in source
    assert "uv run pytest" not in source
    assert "uv run ruff" not in source
    assert "uv run mypy" not in source
    assert "npm ci" not in source
    assert "npm test" not in source
    assert "npm run typecheck" not in source
    assert "不重复执行组件测试，已按风险执行托管 CI 策略" in source


def test_driver_uses_nul_safe_no_rename_diff_for_risk_classification() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "read -r -d '' path" in source
    assert "diff --name-only --no-renames -z" in source
    assert '"$REMOTE_COMMIT" "$TARGET_COMMIT"' in source


def test_driver_reports_remote_root_and_commit_pair_for_preflight_diagnosis() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    preflight = (
        'echo "test-update: preflight root=$REMOTE_ROOT '
        'base=$REMOTE_COMMIT target=$TARGET_COMMIT ref=$REF"'
    )

    assert preflight in source
    assert source.index(preflight) < source.index('BASELINE_STATUS="$(')
    assert (
        '差异分类失败 root=$REMOTE_ROOT base=${REMOTE_COMMIT:0:12} '
        'target=${TARGET_COMMIT:0:12} changed=${#CHANGED_PATHS[@]}'
    ) in source


def test_driver_fails_closed_for_repository_or_missing_baseline_mismatch() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "github_repository_identity" in source
    assert 'git -C "$ROOT" remote get-url origin' in source
    assert 'git -C "$REMOTE_ROOT" "$@"' in source
    assert '"$LOCAL_REPOSITORY" != "$REMOTE_REPOSITORY"' in source
    assert "本地与远端 origin 仓库不一致，拒绝跨仓库更新" in source
    assert 'cat-file -e "$REMOTE_COMMIT^{commit}"' in source
    assert "远端基线 commit 不在本地对象库" in source


def test_driver_rejects_cross_repository_update_before_reading_remote_head(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    target = "1" * 40
    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"fetch --prune origin") exit 0 ;;
  *"rev-parse --verify origin/main^{{commit}}") echo "{target}" ;;
  *"remote get-url origin") echo "https://github.com/acme/canonical.git" ;;
  *) echo "unexpected git command: $*" >&2; exit 90 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
case "$*" in
  *"remote get-url origin") echo "git@github.com:acme/archive.git" ;;
  *) echo "unexpected ssh command: $*" >&2; exit 91 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(DRIVER), "--dry-run", "--ref", "origin/main"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "22",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "本地与远端 origin 仓库不一致，拒绝跨仓库更新" in result.stderr
    assert "preflight root=" not in result.stdout


def test_driver_rejects_missing_baseline_instead_of_reporting_no_change(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    target = "1" * 40
    baseline = "2" * 40
    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"fetch --prune origin") exit 0 ;;
  *"rev-parse --verify origin/main^{{commit}}") echo "{target}" ;;
  *"remote get-url origin") echo "https://github.com/acme/canonical.git" ;;
  *"cat-file -e {baseline}^{{commit}}") exit 1 ;;
  *) echo "unexpected git command: $*" >&2; exit 90 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ssh",
        f"""#!/usr/bin/env bash
case "$*" in
  *"remote get-url origin") echo "git@github.com:acme/canonical.git" ;;
  *"rev-parse HEAD") echo "{baseline}" ;;
  *"status --porcelain"*) exit 0 ;;
  *) echo "unexpected ssh command: $*" >&2; exit 91 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(DRIVER), "--dry-run", "--ref", "origin/main"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "22",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "远端基线 commit 不在本地对象库" in result.stderr
    assert "无运行时代码差异" not in result.stdout


def test_driver_never_treats_diff_failure_as_no_change() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "DIFF_STATUS=0" in source
    assert 'if [[ "$DIFF_STATUS" -gt 1 ]]' in source
    assert "无法计算远端基线到目标 commit 的差异" in source
    assert "差异存在但路径枚举为空，拒绝继续" in source


def test_driver_rejects_public_snapshot_cutover_without_private_object_work() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "--public-snapshot-cutover" in source
    assert "public snapshot cutover 已禁用" in source
    assert "PUBLIC_SNAPSHOT_CUTOVER" not in source
    assert "CUTOVER_PACK_FILE" not in source
    assert "pack-objects --stdout" not in source
    assert 'payload["public_cutover"]' not in source
    assert "deploy/scripts/test_update_contract.py" in source
    assert "classify-nul" in source

    result = subprocess.run(
        [
            "bash",
            str(DRIVER),
            "--public-snapshot-cutover",
            "--ref",
            "origin/feature",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "22",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "不得向公开工作区导入私有 Git 对象" in result.stderr


def test_driver_preflights_github_auth_before_mutation() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    preflight = source.split("github_write_preflight() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    for command in (
        '"$COMMAND" != apply',
        '"$COMMAND" != rebaseline',
        '"$COMMAND" != recover-rebaseline-prepare',
        '"$COMMAND" != promote',
    ):
        assert command in preflight
    assert "gh auth status --hostname github.com" in preflight
    assert 'gh api "repos/$LOCAL_REPOSITORY" --jq .full_name' in preflight
    assert "auth token" not in preflight
    assert source.index("github_write_preflight\n") < source.index(
        "remote_sms_compose test-update promote"
    )
    assert source.index("github_write_preflight\n") < source.index(
        "# 固定远端阶段: sms-compose test-update prepare"
    )


def test_rebaseline_is_narrower_than_daily_apply_and_requires_exact_ci_gate() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert (
        "[plan|build|apply|rebaseline|recover-rebaseline-prepare|recover-rebaseline-verify|status|promote]"
        in source
    )
    assert "rebaseline 只允许 origin/main" in source
    assert "merge-base --is-ancestor" in source
    assert "classify-rebaseline-nul" in source
    assert 'CLASSIFY_ACTION="classify-nul"' in source
    assert "--require-full" not in source
    assert "rebaseline 要求服务器迁移头真实前移" in source
    assert 'components=${PLANNED_COMPONENTS[*]}' in source
    assert 'REQUEST_OPERATION="apply"' in source
    assert 'REQUEST_OPERATION="rebaseline"' in source
    assert '"operation":operation' in source
    assert "remote_bootstrap_sms_compose test-update recover-rebaseline-prepare" in source
    assert source.index('if [[ "$COMMAND" == recover-rebaseline-prepare ]]') < source.index(
        'docker buildx build --platform linux/amd64 --load'
    )

    result = subprocess.run(
        ["bash", str(DRIVER), "rebaseline", "--ref", "origin/feature"],
        cwd=ROOT,
        env={
            **os.environ,
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "22",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "rebaseline 只允许 origin/main" in result.stderr


def test_driver_classifier_accepts_g2_api_acceptance_as_non_runtime() -> None:
    result = subprocess.run(
        [sys.executable, str(CONTRACT), "classify-nul"],
        input=b"scripts/e2e_api.py\0backend/tests/test_e2e_api.py\0",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        "components": [],
        "high_risk_paths": [],
        "migration_changed": False,
        "risk": "none",
        "runtime_changed": False,
    }


def test_driver_allows_only_old_critical_update_pause_to_reach_server_takeover() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert 'status in {"setup_required","inactive","controlled","blocked"}' in source
    assert 'status == "blocked"' in source
    assert 'value.get("pause_kind") == "critical"' in source


def test_driver_explicitly_propagates_fixed_host_control_environment_to_sudo() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "REMOTE_CONTROL_ENV=(" in source
    assert '"/usr/bin/env"' in source
    assert '"SMS_PLATFORM_ROOT=$REMOTE_ROOT"' in source
    assert '"SMS_SECRETS_MODE=development"' in source
    assert '"SMS_RUNTIME_ROOT=/run/sms-platform/secrets"' in source
    assert (
        '"SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials"'
        in source
    )
    assert 'REMOTE_CONTROL_ENV+=("SMS_VENDOR_LIVE_TEST_ORIGIN=$VENDOR_ORIGIN")' in source
    assert "SMS_VENDOR_LIVE_TEST_ORIGIN 格式无效" in source
    assert (
        'REMOTE_SMS_COMPOSE="/usr/local/sbin/sms-compose"'
        in source
    )
    assert (
        'sudo "${REMOTE_CONTROL_ENV[@]}" "$REMOTE_SMS_COMPOSE" "$@"'
        in source
    )
    assert "sudo /usr/local/sbin/sms-compose vendor-test status" not in source
    assert "sudo /usr/local/sbin/sms-compose test-update prepare" not in source


def test_rebaseline_verify_recovery_uses_bootstrap_with_configured_origin(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
printf '%s\n' "$*"
""",
    )

    result = subprocess.run(
        ["bash", str(DRIVER), "recover-rebaseline-verify"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "2222",
            "SMS_VENDOR_LIVE_TEST_ORIGIN": "https://vendor.example.invalid",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "-p 2222" in result.stdout
    assert "SMS_VENDOR_LIVE_TEST_ORIGIN=https://vendor.example.invalid" in result.stdout
    assert (
        "/usr/local/libexec/sms-platform/test-secure-access/sms-compose-bootstrap "
        "test-update recover-rebaseline-verify"
    ) in result.stdout


def test_rebaseline_verify_recovery_requires_vendor_origin_before_network(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
exit 99
""",
    )

    result = subprocess.run(
        ["bash", str(DRIVER), "recover-rebaseline-verify"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "22",
            "SMS_VENDOR_LIVE_TEST_ORIGIN": "",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "要求配置 SMS_VENDOR_LIVE_TEST_ORIGIN" in result.stderr


def test_driver_rejects_invalid_vendor_origin_before_network() -> None:
    result = subprocess.run(
        [
            "bash",
            str(DRIVER),
            "--dry-run",
            "--ref",
            "origin/main",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "SMS_DOCKER_PUBLIC_SESSION": "1",
            "SMS_TEST_UPDATE_TARGET": "operator@test-host",
            "SMS_TEST_UPDATE_PORT": "22",
            "SMS_VENDOR_LIVE_TEST_ORIGIN": "https://vendor.example.invalid/path",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "test-update: SMS_VENDOR_LIVE_TEST_ORIGIN 格式无效"


def test_driver_upload_uses_private_staging_and_fixed_resumable_partial() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    upload = source.split('REMOTE_INCOMING="', maxsplit=1)[1]

    assert "scp " not in upload
    assert 'REMOTE_STAGING="/tmp/sms-test-update-upload.$TARGET_COMMIT"' in upload
    assert (
        '"$TARGET" "env LC_ALL=C stat -c \'%u|%a|%F\' -- '
        "'$REMOTE_STAGING'\""
    ) in upload
    assert 'REMOTE_PART="$REMOTE_STAGING/$name.part"' in upload
    assert "rsync" in upload
    assert '"--partial"' in upload
    assert '"--inplace"' in upload
    assert '"--chmod=Fu=rw,Fgo="' in upload
    assert '"$TARGET:$REMOTE_PART"' in upload
    assert 'for attempt in 1 2 3; do' in upload
    assert "mktemp -d /tmp/sms-test-update-upload." not in upload
    assert 'REMOTE_STAGING="/tmp/$name.part"' not in upload
    assert '"$TARGET:/tmp/$name.part"' not in upload


def test_driver_verifies_each_upload_before_root_atomic_publish() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    upload = source.split('REMOTE_INCOMING="', maxsplit=1)[1]

    remote_hash = upload.index('sha256sum -- "$REMOTE_PART"')
    compare_hash = upload.index('[[ "$REMOTE_SHA256" == "$EXPECTED_SHA256" ]]')
    root_install = upload.index(
        'sudo install -o root -g root -m 0600 -- "$REMOTE_PART" '
        '"$REMOTE_PUBLISH_TEMP"'
    )
    atomic_rename = upload.index(
        'sudo mv -- "$REMOTE_PUBLISH_TEMP" "$REMOTE_INCOMING/$name"'
    )

    assert remote_hash < compare_hash < root_install < atomic_rename
    assert (
        'REMOTE_PUBLISH_TEMP="$REMOTE_INCOMING/.${name}.'
        '${EXPECTED_SHA256}.${REMOTE_UPLOAD_ID}.tmp"'
    ) in upload


def test_driver_publishes_request_last_and_only_cleans_staging_after_success() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    upload = source.split('REMOTE_INCOMING="', maxsplit=1)[1]

    artifacts = upload.index('UPLOAD_ARTIFACTS+=("$BUNDLE/request.json")')
    loop = upload.index('for artifact in "${UPLOAD_ARTIFACTS[@]}"; do')
    cleanup = upload.index('rmdir -- "$REMOTE_STAGING"')
    publish = upload.index(
        'sudo mv -- "$REMOTE_PUBLISH_TEMP" "$REMOTE_INCOMING/$name"'
    )

    assert artifacts < loop < publish < cleanup
    assert 'trap remote_upload_cleanup' not in upload
