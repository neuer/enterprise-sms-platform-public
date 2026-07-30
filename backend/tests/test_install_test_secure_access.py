from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


def installer_module() -> ModuleType:
    try:
        return importlib.import_module("install_test_secure_access")
    except ModuleNotFoundError:
        pytest.fail("test secure access installer is not implemented")


def amd64_elf(payload: bytes = b"fixed-cloudflared") -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = (62).to_bytes(2, "little")
    return bytes(header) + payload


class FakeRunner:
    def __init__(self, *, version: str = "2026.7.2") -> None:
        self.version = version
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.active_state = "inactive"
        self.enabled_state = "static"
        self.fail_disable = False
        self.fail_reset = False
        self.fail_python_runtime = False
        self.unit_exists = False

    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, check))
        if argv[-1:] == ("--version",):
            result = subprocess.CompletedProcess(
                argv,
                0,
                f"cloudflared version {self.version} (built test)\n",
                "",
            )
        elif argv[:3] == ("systemctl", "disable", "--now"):
            result = subprocess.CompletedProcess(
                argv,
                1 if self.fail_disable or not self.unit_exists else 0,
                "",
                "",
            )
        elif argv[:2] == ("systemctl", "reset-failed"):
            self.active_state = "inactive"
            result = subprocess.CompletedProcess(
                argv,
                1 if self.fail_reset else 0,
                "",
                "",
            )
        elif argv[:2] == ("systemctl", "is-active"):
            result = subprocess.CompletedProcess(
                argv,
                0 if self.active_state == "active" else 3,
                f"{self.active_state}\n",
                "",
            )
        elif argv[:2] == ("systemctl", "is-enabled"):
            result = subprocess.CompletedProcess(
                argv,
                0 if self.enabled_state == "static" else 1,
                f"{self.enabled_state}\n",
                "",
            )
        elif argv[:2] == ("/usr/bin/python3", "-I"):
            result = subprocess.CompletedProcess(
                argv,
                1 if self.fail_python_runtime else 0,
                "",
                "",
            )
        else:
            result = subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ("systemctl", "daemon-reload"):
            self.unit_exists = True
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result


class FakeCommitVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def verify(
        self,
        *,
        commit: str,
        path: str,
        sha256: str,
        git_mode: str,
    ) -> None:
        self.calls.append((commit, path, sha256, git_mode))


def fixture(tmp_path: Path):
    module = installer_module()
    root = tmp_path / "platform"
    root.mkdir(mode=0o700)
    source_unit = root / "deploy/systemd/sms-platform-test-secure-access.service"
    source_unit.parent.mkdir(parents=True)
    source_unit.write_text("[Service]\nExecStart=fixed\n", encoding="utf-8")
    source_unit.chmod(0o644)
    for name in (
        "install_test_secure_access.py",
        "test_secure_access_contract.py",
        "test_secure_access_runtime.py",
        "test_secure_access_manager.py",
        "vendor_test_files.py",
        "check_test_update_migration.py",
        "run_with_lifecycle_lock.py",
        "public_cutover_bootstrap.py",
        "test_update_apply.py",
        "test_update_backup.py",
        "test_update_contract.py",
        "test_update_manager.py",
        "test_update_promote.py",
        "test_update_store.py",
        "test_update_verify.py",
    ):
        source = root / "deploy/scripts" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"# fixed {name}\n", encoding="utf-8")
        source.chmod(0o644)
    for name in (
        "check_public_readiness.py",
        "export_public_snapshot.py",
        "verify_public_snapshot_cutover.py",
    ):
        source = root / "scripts" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"# fixed {name}\n", encoding="utf-8")
        source.chmod(0o644)
    wrapper = root / "deploy/sms-compose"
    wrapper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    source_binary = tmp_path / "upload/cloudflared-linux-amd64"
    source_binary.parent.mkdir()
    source_binary.write_bytes(amd64_elf())
    source_binary.chmod(0o644)
    binary_path = tmp_path / "installed/bin/cloudflared"
    installed_unit = (
        tmp_path / "installed/unit/sms-platform-test-secure-access.service"
    )
    host_asset_root = tmp_path / "installed/libexec/test-secure-access"
    backup_config_path = tmp_path / "installed/etc/test-update-backup.json"
    backup_key_path = tmp_path / "installed/etc/test-update-backup-key"
    backup_output_root = tmp_path / "installed/state/test-backups"
    lifecycle_lock_path = tmp_path / "installed/run/secrets.lifecycle.lock"
    runner = FakeRunner()
    commit_verifier = FakeCommitVerifier()
    installer = module.SecureAccessInstaller(
        root=root,
        source_binary=source_binary,
        binary_path=binary_path,
        installed_unit=installed_unit,
        host_asset_root=host_asset_root,
        marker_path=tmp_path / "installed/etc/test-host",
        backup_config_path=backup_config_path,
        backup_key_path=backup_key_path,
        backup_output_root=backup_output_root,
        lifecycle_lock_path=lifecycle_lock_path,
        expected_uid=os.geteuid(),
        expected_sha256=hashlib.sha256(source_binary.read_bytes()).hexdigest(),
        runner=runner,
        source_commit="a" * 40,
        commit_verifier=commit_verifier,
    )
    return module, installer, runner, source_binary, binary_path, installed_unit


def test_git_commit_verifier_binds_blob_type_and_tree_mode(tmp_path: Path) -> None:
    module = installer_module()
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*argv: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *argv],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    source = repository / "asset.py"
    source.write_text("linked-target", encoding="utf-8")
    source.chmod(0o644)
    git("add", "asset.py")
    git("commit", "-m", "regular")
    regular_commit = git("rev-parse", "HEAD")
    digest = hashlib.sha256(b"linked-target").hexdigest()
    verifier = module.GitCommitVerifier(repository)

    verifier.verify(
        commit=regular_commit,
        path="asset.py",
        sha256=digest,
        git_mode="100644",
    )
    tree_object = git("rev-parse", "HEAD^{tree}")
    with pytest.raises(
        module.SecureAccessInstallerError,
        match="source commit",
    ):
        verifier.verify(
            commit=tree_object,
            path="asset.py",
            sha256=digest,
            git_mode="100644",
        )

    source.unlink()
    source.symlink_to("linked-target")
    git("add", "asset.py")
    git("commit", "-m", "symlink")
    symlink_commit = git("rev-parse", "HEAD")
    with pytest.raises(
        module.SecureAccessInstallerError,
        match="source commit",
    ):
        verifier.verify(
            commit=symlink_commit,
            path="asset.py",
            sha256=digest,
            git_mode="100644",
        )


def test_first_install_status_uses_fixed_manager_before_checkout_update() -> None:
    readme = (ROOT / "deploy/README.md").read_text(encoding="utf-8")
    install_section = readme.split("### 一次性主机安装", maxsplit=1)[1].split(
        "### 手机操作者日常流程",
        maxsplit=1,
    )[0]

    assert (
        "/usr/local/libexec/sms-platform/test-secure-access/"
        "test_secure_access_manager.py status"
    ) in install_section
    assert "/usr/local/sbin/sms-compose secure-access status" not in install_section
    assert (
        'SOURCE_ROOT="/var/lib/sms-platform/'
        'test-secure-access-bootstrap/source-$TARGET_COMMIT"'
    ) in install_section
    assert "deploy/scripts deploy/sms-compose" not in install_section
    assert "deploy/scripts/install_test_secure_access.py" in install_section
    assert "deploy/scripts/test_update_manager.py" in install_section


def test_first_install_normalizes_git_archive_modes_before_installer() -> None:
    readme = (ROOT / "deploy/README.md").read_text(encoding="utf-8")
    install_section = readme.split("### 一次性主机安装", maxsplit=1)[1].split(
        "### 手机操作者日常流程",
        maxsplit=1,
    )[0]

    archive_end = install_section.index(
        'sudo /bin/tar -x -C "$SOURCE_ROOT"'
    )
    installer_start = install_section.rindex(
        '"$SOURCE_ROOT/deploy/scripts/install_test_secure_access.py"'
    )
    normalized_section = install_section[archive_end:installer_start]

    assert "sudo chmod 0644 \\" in normalized_section
    for path in (
        "deploy/scripts/install_test_secure_access.py",
        "deploy/scripts/test_secure_access_contract.py",
        "deploy/scripts/test_secure_access_runtime.py",
        "deploy/scripts/test_secure_access_manager.py",
        "deploy/scripts/vendor_test_files.py",
        "deploy/scripts/check_test_update_migration.py",
        "deploy/scripts/run_with_lifecycle_lock.py",
        "deploy/scripts/test_update_apply.py",
        "deploy/scripts/test_update_backup.py",
        "deploy/scripts/test_update_contract.py",
        "deploy/scripts/test_update_manager.py",
        "deploy/scripts/test_update_promote.py",
        "deploy/scripts/test_update_store.py",
        "deploy/scripts/test_update_verify.py",
        "scripts/check_public_readiness.py",
        "scripts/export_public_snapshot.py",
        "scripts/verify_public_snapshot_cutover.py",
        "deploy/systemd/sms-platform-test-secure-access.service",
    ):
        assert f'"$SOURCE_ROOT/{path}"' in normalized_section
    assert (
        'sudo chmod 0755 "$SOURCE_ROOT/deploy/sms-compose"'
        in normalized_section
    )


def test_installer_atomically_installs_pinned_binary_and_static_unit(
    tmp_path: Path,
) -> None:
    module, installer, runner, source, binary, unit = fixture(tmp_path)

    result = installer.run()

    assert result.as_dict() == {
        "status": "installed",
        "service": "sms-platform-test-secure-access.service",
        "version": "2026.7.2",
    }
    assert binary.read_bytes() == source.read_bytes()
    assert binary.stat().st_mode & 0o777 == 0o755
    assert binary.stat().st_uid == os.geteuid()
    assert unit.read_text(encoding="utf-8") == "[Service]\nExecStart=fixed\n"
    assert unit.stat().st_mode & 0o777 == 0o644
    assert unit.stat().st_uid == os.geteuid()
    manifest = installer.host_asset_root / "manifest.json"
    assert manifest.stat().st_mode & 0o777 == 0o644
    from test_secure_access_contract import HOST_ASSET_NAMES, parse_host_manifest

    parsed = parse_host_manifest(manifest.read_text(encoding="utf-8"))
    assert parsed.source_commit == "a" * 40
    digests = parsed.files
    assert set(digests) == set(HOST_ASSET_NAMES)
    for name in HOST_ASSET_NAMES:
        path = (
            binary
            if name == "cloudflared"
            else installer.host_asset_root / name
        )
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digests[name]
        if name == "sms-compose-bootstrap":
            assert path.stat().st_mode & 0o777 == 0o755
        elif name != "cloudflared":
            assert path.stat().st_mode & 0o777 == 0o644
    assert list(binary.parent.glob(".*.tmp")) == []
    assert list(unit.parent.glob(".*")) == []

    commands = [argv for argv, _check in runner.calls]
    python_runtime_command = next(
        argv for argv in commands if argv[:2] == ("/usr/bin/python3", "-I")
    )
    import_root = Path(python_runtime_command[-1])
    assert import_root.parent == installer.host_asset_root
    assert import_root.name.startswith(".python-import-")
    assert "sys.version_info < (3, 11)" in python_runtime_command[3]
    assert "import test_secure_access_manager" in python_runtime_command[3]
    assert "import public_cutover_bootstrap" in python_runtime_command[3]
    assert "import test_update_manager" in python_runtime_command[3]
    assert "import verify_public_snapshot_cutover" in python_runtime_command[3]
    version_command = next(argv for argv in commands if argv[-1:] == ("--version",))
    assert Path(version_command[0]).parent == binary.parent
    assert version_command[0] != str(source)
    assert (
        "systemctl",
        "disable",
        "--now",
        "sms-platform-test-secure-access.service",
    ) not in commands
    verify_command = next(
        argv for argv in commands if argv[:2] == ("systemd-analyze", "verify")
    )
    assert Path(verify_command[2]).suffix == ".service"
    assert ("systemctl", "daemon-reload") in commands
    from test_secure_access_contract import parse_test_host_marker

    assert installer.marker_path.stat().st_mode & 0o777 == 0o600
    assert (
        parse_test_host_marker(installer.marker_path.read_text(encoding="utf-8"))
        is None
    )
    forbidden_actions = {("systemctl", "start"), ("systemctl", "enable")}
    assert not any(argv[:2] in forbidden_actions for argv in commands)


def test_installer_bootstraps_encrypted_checkpoint_prerequisites_once(
    tmp_path: Path,
) -> None:
    _, installer, _, _, _, _ = fixture(tmp_path)

    installer.run()

    config = installer.backup_config_path
    key = installer.backup_key_path
    output = installer.backup_output_root
    assert config.read_text(encoding="utf-8") == (
        '{"database":"sms","key_file":"'
        f'{key}","output_root":"{output}","schema_version":1}}\n'
    )
    assert config.stat().st_mode & 0o777 == 0o600
    assert key.stat().st_mode & 0o777 == 0o600
    assert len(key.read_bytes()) == 32
    assert output.stat().st_mode & 0o777 == 0o700
    original_key = key.read_bytes()

    installer.run()

    assert key.read_bytes() == original_key


def test_installer_blocks_checkpoint_bootstrap_while_lifecycle_lock_is_held(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.lifecycle_lock_path.parent.mkdir(parents=True, mode=0o700)
    installer.lifecycle_lock_path.parent.chmod(0o700)
    descriptor = os.open(
        installer.lifecycle_lock_path,
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(
            module.SecureAccessInstallerError,
            match="checkpoint prerequisites",
        ):
            installer.run()
    finally:
        os.close(descriptor)

    assert not installer.backup_config_path.exists()
    assert not installer.backup_key_path.exists()


def test_installer_rejects_config_without_checkpoint_key(
    tmp_path: Path,
) -> None:
    module, installer, _, _, binary, unit = fixture(tmp_path)
    installer.backup_config_path.parent.mkdir(parents=True)
    installer.backup_config_path.write_text(
        '{"database":"sms","key_file":"'
        f'{installer.backup_key_path}","output_root":"'
        f'{installer.backup_output_root}","schema_version":1}}\n',
        encoding="utf-8",
    )
    installer.backup_config_path.chmod(0o600)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert not installer.backup_key_path.exists()
    assert not binary.exists()
    assert not unit.exists()


def test_installer_rejects_boolean_checkpoint_schema_version(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.backup_config_path.parent.mkdir(parents=True)
    installer.backup_config_path.write_text(
        '{"database":"sms","key_file":"'
        f'{installer.backup_key_path}","output_root":"'
        f'{installer.backup_output_root}","schema_version":true}}\n',
        encoding="utf-8",
    )
    installer.backup_config_path.chmod(0o600)
    installer.backup_key_path.write_bytes(b"k" * 32)
    installer.backup_key_path.chmod(0o600)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()


def test_installer_recovers_key_only_when_checkpoint_root_is_empty(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.backup_key_path.parent.mkdir(parents=True)
    installer.backup_key_path.write_bytes(b"k" * 32)
    installer.backup_key_path.chmod(0o600)
    installer.backup_output_root.mkdir(parents=True, mode=0o700)

    installer.run()

    assert installer.backup_config_path.is_file()
    assert installer.backup_key_path.read_bytes() == b"k" * 32

    installer.backup_config_path.unlink()
    (installer.backup_output_root / "existing-checkpoint").mkdir()
    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()


def test_installer_never_generates_key_over_orphaned_checkpoints(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    checkpoint = installer.backup_output_root / "orphaned-checkpoint"
    checkpoint.mkdir(parents=True, mode=0o700)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert not installer.backup_key_path.exists()
    assert not installer.backup_config_path.exists()


def test_installer_never_overwrites_concurrently_created_checkpoint_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    real_link = module.os.link
    competing_key = b"r" * 32

    def racing_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(destination) == installer.backup_key_path:
            installer.backup_key_path.write_bytes(competing_key)
            installer.backup_key_path.chmod(0o600)
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(module.os, "link", racing_link)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert installer.backup_key_path.read_bytes() == competing_key
    assert not installer.backup_config_path.exists()


def test_installer_binds_committed_key_to_staged_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    real_link = module.os.link

    def replace_key_after_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(source, destination, follow_symlinks=follow_symlinks)
        if Path(destination) == installer.backup_key_path:
            replacement = installer.backup_key_path.with_suffix(".replacement")
            replacement.write_bytes(Path(source).read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, installer.backup_key_path)

    monkeypatch.setattr(module.os, "link", replace_key_after_link)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert installer.backup_key_path.is_file()
    assert not installer.backup_config_path.exists()


def test_installer_rejects_incomplete_checkpoint_with_authority_present(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.run()
    (installer.backup_output_root / ".interrupted.tmp").mkdir(mode=0o700)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()


def test_installer_accepts_complete_encrypted_checkpoint(
    tmp_path: Path,
) -> None:
    _, installer, _, _, _, _ = fixture(tmp_path)
    installer.run()
    checkpoint_id = "test-update-20260720T000000Z-abcdef123456"
    checkpoint = installer.backup_output_root / checkpoint_id
    checkpoint.mkdir(mode=0o700)
    ciphertext = checkpoint / "database.dump.aesgcm"
    ciphertext.write_bytes(b"encrypted-payload")
    ciphertext.chmod(0o600)
    manifest = checkpoint / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checkpoint_id": checkpoint_id,
                "created_at": "2026-07-20T00:00:00+00:00",
                "database": "sms",
                "cipher": "AES-256-GCM",
                "aad": "sms-test-update-v1",
                "nonce": "00" * 12,
                "ciphertext_file": "database.dump.aesgcm",
                "restore_readable": True,
                "complete": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)

    installer.run()


def test_installer_rejects_checkpoint_root_mode_drift_without_repair(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.run()
    installer.backup_output_root.chmod(0o755)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert installer.backup_output_root.stat().st_mode & 0o777 == 0o755


def test_installer_rejects_checkpoint_key_with_additional_hardlink(
    tmp_path: Path,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.run()
    os.link(installer.backup_key_path, installer.backup_key_path.with_suffix(".copy"))

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()


@pytest.mark.parametrize("target_name", ["backup_config_path", "backup_key_path"])
def test_installer_revalidates_existing_checkpoint_authority_after_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.run()
    target = getattr(installer, target_name)
    original = target.read_bytes()
    validate = installer._validate_completed_checkpoints

    def replace_authority_after_traversal() -> None:
        validate()
        replacement = target.with_suffix(f"{target.suffix}.replacement")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        os.replace(replacement, target)

    monkeypatch.setattr(
        installer,
        "_validate_completed_checkpoints",
        replace_authority_after_traversal,
    )

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()


def test_installer_binds_committed_config_to_staged_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.backup_key_path.parent.mkdir(parents=True)
    installer.backup_key_path.write_bytes(b"k" * 32)
    installer.backup_key_path.chmod(0o600)
    installer.backup_output_root.mkdir(parents=True, mode=0o700)
    real_link = module.os.link

    def replace_config_after_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(source, destination, follow_symlinks=follow_symlinks)
        if Path(destination) == installer.backup_config_path:
            replacement = installer.backup_config_path.with_suffix(".replacement")
            replacement.write_bytes(Path(source).read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, installer.backup_config_path)

    monkeypatch.setattr(module.os, "link", replace_config_after_link)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert installer.backup_config_path.is_file()
    assert installer.backup_key_path.read_bytes() == b"k" * 32
    monkeypatch.setattr(module.os, "link", real_link)

    installer.run()


def test_installer_revalidates_key_around_config_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.backup_key_path.parent.mkdir(parents=True)
    installer.backup_key_path.write_bytes(b"k" * 32)
    installer.backup_key_path.chmod(0o600)
    installer.backup_output_root.mkdir(parents=True, mode=0o700)
    real_link = module.os.link

    def replace_key_before_config_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(destination) == installer.backup_config_path:
            replacement = installer.backup_key_path.with_suffix(".replacement")
            replacement.write_bytes(b"s" * 32)
            replacement.chmod(0o600)
            os.replace(replacement, installer.backup_key_path)
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(module.os, "link", replace_key_before_config_link)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer.run()

    assert installer.backup_config_path.is_file()
    assert installer.backup_key_path.read_bytes() == b"s" * 32
    monkeypatch.setattr(module.os, "link", real_link)

    installer.run()


def test_installer_repairs_directory_durability_after_postcommit_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, installer, _, _, _, _ = fixture(tmp_path)
    installer.backup_key_path.parent.mkdir(parents=True)
    installer.backup_key_path.write_bytes(b"k" * 32)
    installer.backup_key_path.chmod(0o600)
    installer.backup_output_root.mkdir(parents=True, mode=0o700)
    real_fsync_directory = module._fsync_directory
    attempts = 0

    def fail_first_directory_fsync(path: Path) -> None:
        nonlocal attempts
        if (
            path == installer.backup_config_path.parent
            and installer.backup_config_path.exists()
        ):
            attempts += 1
            if attempts == 1:
                raise OSError("injected directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(module, "_fsync_directory", fail_first_directory_fsync)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checkpoint prerequisites",
    ):
        installer._ensure_checkpoint_prerequisites()

    assert installer.backup_config_path.is_file()

    installer._ensure_checkpoint_prerequisites()

    assert attempts == 2


def test_installer_makes_host_asset_parent_traversable_without_listing(
    tmp_path: Path,
) -> None:
    _, installer, _, _, _, _ = fixture(tmp_path)
    libexec = installer.host_asset_root.parent
    libexec.mkdir(mode=0o750, parents=True)
    libexec.chmod(0o750)

    installer.run()

    mode = libexec.stat().st_mode & 0o777
    assert mode == 0o751


def test_installer_does_not_expose_asset_parent_before_source_verification(
    tmp_path: Path,
) -> None:
    module, installer, _, source, _, _ = fixture(tmp_path)
    libexec = installer.host_asset_root.parent
    libexec.mkdir(mode=0o750, parents=True)
    libexec.chmod(0o750)
    source.write_bytes(amd64_elf(b"drift"))
    source.chmod(0o644)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="checksum",
    ):
        installer.run()

    assert libexec.stat().st_mode & 0o777 == 0o750


def test_installer_rejects_incomplete_fixed_host_python_before_mutation(
    tmp_path: Path,
) -> None:
    module, installer, runner, _, binary, unit = fixture(tmp_path)
    runner.fail_python_runtime = True

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="host Python dependency is unavailable",
    ):
        installer.run()

    assert not binary.exists()
    assert not unit.exists()
    assert not installer.marker_path.exists()
    assert not installer.manifest_path.exists()


def test_installer_never_imports_host_modules_before_every_commit_blob_is_verified(
    tmp_path: Path,
) -> None:
    module, installer, runner, _, _, _ = fixture(tmp_path)

    class RejectingVerifier:
        def __init__(self) -> None:
            self.calls = 0

        def verify(
            self,
            *,
            commit: str,
            path: str,
            sha256: str,
            git_mode: str,
        ) -> None:
            del commit, path, sha256, git_mode
            self.calls += 1
            raise module.SecureAccessInstallerError("injected commit mismatch")

    verifier = RejectingVerifier()
    installer.commit_verifier = verifier

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="injected commit mismatch",
    ):
        installer.run()

    assert verifier.calls == 1
    assert not any(
        argv[:2] == ("/usr/bin/python3", "-I")
        for argv, _check in runner.calls
    )


def test_installer_rejects_existing_unit_symlink_before_systemd_mutation(
    tmp_path: Path,
) -> None:
    module, installer, runner, _, _, unit = fixture(tmp_path)
    unit.parent.mkdir(parents=True)
    target = unit.with_suffix(".real")
    target.write_text("[Service]\n", encoding="utf-8")
    target.chmod(0o644)
    unit.symlink_to(target)

    with pytest.raises(
        module.SecureAccessInstallerError,
        match="service unit is unsafe",
    ):
        installer.run()

    commands = [argv for argv, _check in runner.calls]
    assert (
        "systemctl",
        "disable",
        "--now",
        "sms-platform-test-secure-access.service",
    ) not in commands


@pytest.mark.parametrize(
    "unsafe",
    [
        "symlink",
        "writable",
        "sha",
        "elf-class",
        "elf-machine",
        "version",
        "source-root-writable",
        "source-root-owner",
    ],
)
def test_installer_rejects_unsafe_or_unpinned_source_before_systemd_mutation(
    tmp_path: Path,
    unsafe: str,
) -> None:
    module, installer, runner, source, _, _ = fixture(tmp_path)
    if unsafe == "symlink":
        target = source.with_suffix(".real")
        source.rename(target)
        source.symlink_to(target)
    elif unsafe == "writable":
        source.chmod(0o664)
    elif unsafe == "sha":
        source.write_bytes(amd64_elf(b"drift"))
        source.chmod(0o644)
    elif unsafe == "elf-class":
        data = bytearray(source.read_bytes())
        data[4] = 1
        source.write_bytes(data)
        source.chmod(0o644)
        installer.expected_sha256 = hashlib.sha256(data).hexdigest()
    elif unsafe == "elf-machine":
        data = bytearray(source.read_bytes())
        data[18:20] = (183).to_bytes(2, "little")
        source.write_bytes(data)
        source.chmod(0o644)
        installer.expected_sha256 = hashlib.sha256(data).hexdigest()
    elif unsafe == "version":
        runner.version = "2026.7.3"
    elif unsafe == "source-root-writable":
        installer.root.chmod(0o777)
    else:
        installer.expected_uid = os.geteuid() + 1

    with pytest.raises(module.SecureAccessInstallerError, match="install"):
        installer.run()

    assert not any(argv[0] == "systemctl" for argv, _ in runner.calls)
    assert not installer.binary_path.exists()
    assert not installer.installed_unit.exists()


def test_installer_rejects_staged_assets_not_bound_to_target_commit(
    tmp_path: Path,
) -> None:
    module, installer, runner, _, binary, unit = fixture(tmp_path)

    class RejectingVerifier:
        def verify(
            self,
            *,
            commit: str,
            path: str,
            sha256: str,
            git_mode: str,
        ) -> None:
            del commit, path, sha256, git_mode
            raise module.SecureAccessInstallerError("source commit mismatch")

    installer.commit_verifier = RejectingVerifier()

    with pytest.raises(module.SecureAccessInstallerError, match="source commit"):
        installer.run()

    assert not binary.exists()
    assert not unit.exists()
    assert not any(argv[0] == "systemctl" for argv, _ in runner.calls)


def test_installer_verifies_unit_before_replacing_existing_assets(
    tmp_path: Path,
) -> None:
    module, installer, runner, _, binary, unit = fixture(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old-binary")
    unit.parent.mkdir(parents=True)
    unit.write_text("old-unit\n", encoding="utf-8")
    runner.unit_exists = True

    class FailingVerifyRunner(FakeRunner):
        def run(
            self,
            *argv: str,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ("systemd-analyze", "verify"):
                raise subprocess.CalledProcessError(1, argv)
            return super().run(*argv, check=check)

    installer.runner = FailingVerifyRunner()

    with pytest.raises(module.SecureAccessInstallerError, match="install"):
        installer.run()

    assert binary.read_bytes() == b"old-binary"
    assert unit.read_text(encoding="utf-8") == "old-unit\n"


def test_installer_aborts_when_existing_unit_cannot_be_disabled_and_stopped(
    tmp_path: Path,
) -> None:
    module, installer, runner, _, binary, unit = fixture(tmp_path)
    unit.parent.mkdir(parents=True)
    unit.write_text("old-unit\n", encoding="utf-8")
    unit.chmod(0o644)
    runner.unit_exists = True
    runner.fail_disable = True

    with pytest.raises(module.SecureAccessInstallerError, match="install"):
        installer.run()

    assert not binary.exists()
    assert unit.read_text(encoding="utf-8") == "old-unit\n"


def test_installer_repairs_failed_static_unit_before_reinstall(
    tmp_path: Path,
) -> None:
    _, installer, runner, _, _, unit = fixture(tmp_path)
    unit.parent.mkdir(parents=True)
    unit.write_text("old-unit\n", encoding="utf-8")
    unit.chmod(0o644)
    runner.unit_exists = True
    runner.active_state = "failed"

    installer.run()

    commands = [argv for argv, _check in runner.calls]
    disable_index = commands.index(
        (
            "systemctl",
            "disable",
            "--now",
            "sms-platform-test-secure-access.service",
        )
    )
    reset_index = commands.index(
        (
            "systemctl",
            "reset-failed",
            "sms-platform-test-secure-access.service",
        )
    )
    assert disable_index < reset_index


def test_installer_accepts_reset_failed_not_loaded_after_confirmed_stop(
    tmp_path: Path,
) -> None:
    _, installer, runner, _, _, unit = fixture(tmp_path)
    unit.parent.mkdir(parents=True)
    unit.write_text("old-unit\n", encoding="utf-8")
    unit.chmod(0o644)
    runner.unit_exists = True
    runner.fail_reset = True

    installer.run()

    assert (
        (
            "systemctl",
            "reset-failed",
            "sms-platform-test-secure-access.service",
        ),
        False,
    ) in runner.calls


@pytest.mark.parametrize(
    ("active_state", "enabled_state"),
    [("active", "static"), ("inactive", "enabled")],
)
def test_installer_refuses_to_report_installed_without_inactive_static_unit(
    tmp_path: Path,
    active_state: str,
    enabled_state: str,
) -> None:
    module, installer, runner, _, _, _ = fixture(tmp_path)
    runner.active_state = active_state
    runner.enabled_state = enabled_state

    with pytest.raises(module.SecureAccessInstallerError, match="install"):
        installer.run()


@pytest.mark.parametrize(
    "argv,euid",
    [
        ([], 0),
        (["--cloudflared-file"], 0),
        (["--cloudflared-file", "relative"], 0),
        (["--cloudflared-file", "/tmp/file", "--origin", "http://evil"], 0),
        (["--url", "https://example.test"], 0),
        (["--cloudflared-file", "/tmp/file"], 501),
    ],
)
def test_installer_cli_rejects_non_root_and_arbitrary_arguments(
    argv: list[str],
    euid: int,
) -> None:
    module = installer_module()

    with pytest.raises(module.SecureAccessInstallerError, match="invocation"):
        module.parse_install_source(argv, euid=euid)


def test_installer_cli_accepts_one_absolute_local_file() -> None:
    module = installer_module()

    assert module.parse_install_source(
        ["--cloudflared-file", "/tmp/cloudflared-linux-amd64"],
        euid=0,
    ) == Path("/tmp/cloudflared-linux-amd64")


def test_installer_cli_accepts_fixed_binary_and_absolute_staged_source_root() -> None:
    module = installer_module()

    assert module.parse_install_invocation(
        [
            "--cloudflared-file",
            "/var/lib/sms-platform/test-secure-access-bootstrap/cloudflared-linux-amd64",
            "--source-root",
            f"/var/lib/sms-platform/test-secure-access-bootstrap/source-{'a' * 40}",
            "--source-commit",
            "a" * 40,
        ],
        euid=0,
    ) == (
        Path(
            "/var/lib/sms-platform/test-secure-access-bootstrap/"
            "cloudflared-linux-amd64"
        ),
        Path(
            "/var/lib/sms-platform/test-secure-access-bootstrap/"
            f"source-{'a' * 40}"
        ),
        "a" * 40,
    )
