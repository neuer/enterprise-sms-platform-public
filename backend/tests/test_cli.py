from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import json
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest

from app.services.crypto import CryptoService, EncryptionContext


def load_cli_module() -> ModuleType:
    assert importlib.util.find_spec("app.cli") is not None, "app.cli seed-dev 尚未实现"
    return importlib.import_module("app.cli")


def development_keys(module: ModuleType) -> dict[str, str]:
    return {
        app.name: f"{app.name}-{'x' * 40}"
        for app in module.DEV_APPS
    }


def test_usage_ledger_cli_requires_safe_stable_reference() -> None:
    module = load_cli_module()
    parser = module.build_parser()

    assert parser.parse_args(["usage-projection-rebuild"]).command == (
        "usage-projection-rebuild"
    )
    reservation_id = UUID("12345678-1234-4234-9234-123456789abc")
    args = parser.parse_args(
        ["usage-ledger-explain", "--reservation-id", str(reservation_id)]
    )
    assert args.reservation_id == reservation_id
    assert args.batch_no is None
    with pytest.raises(SystemExit):
        parser.parse_args(["usage-ledger-explain"])
    assert parser.parse_args(["vendor-event-duplicate-audit"]).command == (
        "vendor-event-duplicate-audit"
    )


def test_seed_manifest_has_fixed_users_apps_template_and_sign() -> None:
    module = load_cli_module()

    assert [(user.username, user.role) for user in module.DEV_USERS] == [
        ("admin01", "admin"),
        ("approver01", "approver"),
        ("operator01", "operator"),
        ("viewer01", "viewer"),
    ]
    assert [(app.name, app.allowed_categories) for app in module.DEV_APPS] == [
        ("app-iam", "verify"),
        ("app-oa", "notice"),
        ("app-mkt", "market"),
    ]
    assert {app.rate_limit_per_min for app in module.DEV_APPS} == {10_000}
    assert module.DEV_TEMPLATE.vendor_state == "approved"
    assert module.DEV_SIGN.vendor_state == "approved"


def test_database_seed_commands_only_contain_key_hashes() -> None:
    module = load_cli_module()
    keys = development_keys(module)
    commands = module.seed_commands(keys)
    serialized_params = json.dumps([params for _, params in commands], ensure_ascii=False)

    assert len(commands) == 10
    assert "UPDATE auth_provider" in commands[0][0]
    assert commands[0][1] == {"provider_code": "ad"}
    mapping_sql, mapping_params = commands[1]
    assert "INSERT INTO external_role_mapping" in mapping_sql
    assert all(
        f"('mock:{role}','{role}')" in mapping_sql
        for role in ("admin", "approver", "operator", "viewer")
    )
    assert mapping_params == {}
    for sql, params in commands[2:6]:
        assert "sys_user" not in sql
        assert "user_account" in sql and "auth_identity" in sql
        assert params["external_subject"] == f"mock:{params['username']}"
    for app in module.DEV_APPS:
        assert keys[app.name] not in serialized_params
        assert hashlib.sha256(keys[app.name].encode()).hexdigest() in serialized_params
    app_sql, app_params = commands[6]
    assert "rate_limit_per_min" in app_sql
    assert "rate_limit_per_min = EXCLUDED.rate_limit_per_min" in app_sql
    assert app_params["rate_limit_per_min"] == 10_000


@pytest.mark.asyncio
async def test_template_seed_persists_only_object_bound_ciphertext() -> None:
    module = load_cli_module()
    crypto = CryptoService(
        aes_keys={1: b"a" * 32},
        hmac_keys={1: b"b" * 32},
        active_version=1,
    )

    class FakeConnection:
        def __init__(self) -> None:
            self.scalars: list[object | None] = [None, 17]
            self.calls: list[tuple[str, object]] = []

        async def scalar(self, statement: object, params: object = None) -> object | None:
            self.calls.append((str(statement), params))
            return self.scalars.pop(0)

        async def execute(self, statement: object, params: object = None) -> None:
            self.calls.append((str(statement), params))

    connection = FakeConnection()
    await module.seed_dev_template(connection, crypto)

    insert_sql, insert_params = connection.calls[-1]
    assert "'[encrypted]'" in insert_sql
    assert "CAST(:dept AS varchar(128))" in insert_sql
    assert "CAST(:vendor_state AS varchar(10))" in insert_sql
    assert "content='[encrypted]'" not in insert_sql
    assert "'[encrypted]'" in insert_sql
    assert module.DEV_TEMPLATE.content not in str(insert_params)
    assert module.DEV_TEMPLATE.name not in str(insert_params)
    assert isinstance(insert_params, dict)
    assert crypto.decrypt_bound_packed_text(
        insert_params["content_enc"],
        EncryptionContext(
            domain="sms-template-content",
            table="sms_template",
            column="content_enc",
            object_id="17",
        ),
    ) == module.DEV_TEMPLATE.content
    assert crypto.decrypt_bound_packed_text(
        insert_params["name_enc"],
        EncryptionContext(
            domain="sms-template-name",
            table="sms_template",
            column="name_enc",
            object_id="17",
        ),
    ) == module.DEV_TEMPLATE.name


def test_api_key_file_is_atomic_json_and_owner_only(tmp_path: Path) -> None:
    module = load_cli_module()
    destination = tmp_path / "nested/dev-apikeys.txt"
    keys = development_keys(module)

    module.write_dev_api_keys(destination, keys)

    assert json.loads(destination.read_text(encoding="utf-8")) == keys
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert list(destination.parent.glob("*.tmp")) == []


def test_api_keys_are_generated_locally_and_safe_existing_values_are_reused(
    tmp_path: Path,
) -> None:
    module = load_cli_module()
    destination = tmp_path / "dev-apikeys.txt"

    generated, was_generated = module.load_or_generate_dev_api_keys(destination)
    assert was_generated is True
    assert set(generated) == {app.name for app in module.DEV_APPS}
    assert all(len(value) >= 32 and not value.startswith("dev_") for value in generated.values())
    module.write_dev_api_keys(destination, generated)

    reused, was_generated = module.load_or_generate_dev_api_keys(destination)
    assert reused == generated
    assert was_generated is False

    destination.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        module.load_or_generate_dev_api_keys(destination)


def test_legacy_fixed_api_key_format_is_rotated_without_echoing_values(
    tmp_path: Path,
) -> None:
    module = load_cli_module()
    destination = tmp_path / "dev-apikeys.txt"
    destination.write_text(
        json.dumps(
            {
                app.name: f"dev_{app.name}_{'x' * 32}"
                for app in module.DEV_APPS
            }
        ),
        encoding="utf-8",
    )
    destination.chmod(0o600)

    generated, was_generated = module.load_or_generate_dev_api_keys(destination)

    assert was_generated is True
    assert all(not value.startswith("dev_") for value in generated.values())


def test_seed_dev_rejects_non_mock_auth() -> None:
    module = load_cli_module()

    with pytest.raises(RuntimeError, match="AUTH_MOCK"):
        module.ensure_seed_allowed(SimpleNamespace(auth_mock=False))


class FakeTty:
    def __init__(self, *, is_tty: bool = True) -> None:
        self.is_tty = is_tty
        self.lines: list[str] = []

    def write_line(self, value: str) -> None:
        self.lines.append(value)


class FakeInitAdminRepository:
    def __init__(self) -> None:
        self.created = False
        self.calls: list[dict[str, object]] = []
        self.lock = asyncio.Lock()

    async def create_initial_admin(self, **kwargs: object) -> None:
        module = load_cli_module()
        async with self.lock:
            if self.created:
                raise module.InitAdminAlreadyCompleted("系统已完成管理员初始化")
            self.created = True
            self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_init_admin_generates_valid_password_and_displays_it_once_after_create() -> None:
    module = load_cli_module()
    repository = FakeInitAdminRepository()
    output = FakeTty()
    service = module.InitAdminService(repository)

    password = await module.run_init_admin(
        service,
        username=" Admin ",
        display_name="系统管理员",
        show_temporary_password=True,
        output=output,
    )

    assert len(password) == 20
    module.PasswordPolicy().validate(password, username="admin")
    assert output.lines == [f"临时密码（仅显示一次）：{password}"]
    assert repository.calls[0]["username"] == "admin"
    assert repository.calls[0]["normalized_login_name"] == "admin"
    assert repository.calls[0]["display_name"] == "系统管理员"
    password_hash = str(repository.calls[0]["password_hash"])
    assert password not in password_hash
    assert password_hash.startswith("$argon2id$")


@pytest.mark.asyncio
async def test_init_admin_requires_display_flag_and_real_tty_before_database_write() -> None:
    module = load_cli_module()
    repository = FakeInitAdminRepository()
    service = module.InitAdminService(repository)

    with pytest.raises(module.TemporaryPasswordDisplayRequired):
        await module.run_init_admin(
            service,
            username="admin",
            display_name="系统管理员",
            show_temporary_password=False,
            output=FakeTty(),
        )
    with pytest.raises(module.ControllingTtyRequired):
        await module.run_init_admin(
            service,
            username="admin",
            display_name="系统管理员",
            show_temporary_password=True,
            output=FakeTty(is_tty=False),
        )

    assert repository.calls == []


@pytest.mark.asyncio
async def test_init_admin_is_single_winner_under_concurrency() -> None:
    module = load_cli_module()
    repository = FakeInitAdminRepository()
    service = module.InitAdminService(repository)
    outputs = (FakeTty(), FakeTty())

    results = await asyncio.gather(
        *(
            module.run_init_admin(
                service,
                username="admin",
                display_name="系统管理员",
                show_temporary_password=True,
                output=output,
            )
            for output in outputs
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(value, str) for value in results) == 1
    assert sum(isinstance(value, module.InitAdminAlreadyCompleted) for value in results) == 1
    assert sum(len(output.lines) for output in outputs) == 1


def test_init_admin_sql_contract_uses_advisory_lock_empty_check_and_no_plaintext() -> None:
    module = load_cli_module()
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "pg_advisory_xact_lock" in source
    assert "SELECT count(*) FROM user_account" in source
    assert "initial_local_admin_create" in source
    assert "password_hash" in source
    assert "password_plaintext" not in source
    assert "BOOTSTRAP_ADMIN_USERS" not in source


def test_init_admin_audit_casts_json_username_for_asyncpg() -> None:
    module = load_cli_module()
    source = inspect.getsource(module.SqlInitAdminRepository.create_initial_admin)

    assert "'username',CAST(:audit_username AS text)" in source
    assert '"audit_username": username' in source


def test_init_admin_parser_accepts_only_generated_password_display_options() -> None:
    module = load_cli_module()
    parser = module.build_parser()

    args = parser.parse_args(
        [
            "init-admin",
            "--username",
            "root.admin",
            "--display-name",
            "平台管理员",
            "--show-temporary-password",
        ]
    )

    assert args.username == "root.admin"
    assert args.display_name == "平台管理员"
    assert args.show_temporary_password is True
    with pytest.raises(SystemExit):
        parser.parse_args(["init-admin", "--password", "UserSupplied@123"])


class FakeHiddenTtyInput:
    def __init__(self, value: str, *, is_tty: bool = True) -> None:
        self.value = value
        self.is_tty = is_tty
        self.prompts: list[str] = []

    def read_hidden(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.value


def _vendor_test_crypto() -> CryptoService:
    return CryptoService(
        aes_keys={1: b"a" * 32},
        hmac_keys={1: b"b" * 32},
        active_version=1,
    )


def test_vendor_test_recipient_reads_phone_from_tty_and_writes_only_hmac(
    tmp_path: Path,
) -> None:
    module = load_cli_module()
    destination = tmp_path / "allowlist.json"
    phone = "13900000001"
    hidden_input = FakeHiddenTtyInput(phone)
    crypto = _vendor_test_crypto()

    count = module.run_vendor_test_recipient(
        action="add",
        hidden_input=hidden_input,
        crypto=crypto,
        destination=destination,
    )

    payload = json.loads(destination.read_text(encoding="utf-8"))
    serialized = destination.read_text(encoding="utf-8")
    assert count == 1
    assert hidden_input.prompts == ["测试手机号："]
    assert payload == {
        "schema_version": 1,
        "entries": [
            {
                "key_version": 1,
                "phone_hmac": crypto.phone_hmac(phone),
            }
        ],
    }
    assert phone not in serialized
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    assert (
        module.run_vendor_test_recipient(
            action="remove",
            hidden_input=FakeHiddenTtyInput(phone),
            crypto=crypto,
            destination=destination,
        )
        == 0
    )
    assert json.loads(destination.read_text(encoding="utf-8"))["entries"] == []


def test_vendor_test_recipient_can_atomically_publish_group_readable_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_cli_module()
    destination = tmp_path / "allowlist.json"
    chown_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.services.vendor_test_guard.os.fchown",
        lambda _fd, uid, gid: chown_calls.append((uid, gid)),
    )

    module.run_vendor_test_recipient(
        action="add",
        hidden_input=FakeHiddenTtyInput("13900000001"),
        crypto=_vendor_test_crypto(),
        destination=destination,
        destination_mode=0o640,
        destination_group_id=10001,
    )

    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert chown_calls == [(-1, 10001)]


def test_vendor_test_recipient_rejects_non_tty_before_read_or_write(tmp_path: Path) -> None:
    module = load_cli_module()
    destination = tmp_path / "allowlist.json"

    with pytest.raises(module.ControllingTtyRequired, match="TTY"):
        module.run_vendor_test_recipient(
            action="add",
            hidden_input=FakeHiddenTtyInput("13900000001", is_tty=False),
            crypto=_vendor_test_crypto(),
            destination=destination,
        )

    assert not destination.exists()


def test_vendor_test_recipient_remove_clears_all_hmac_key_versions(tmp_path: Path) -> None:
    module = load_cli_module()
    destination = tmp_path / "allowlist.json"
    phone = "13900000001"
    crypto = CryptoService(
        aes_keys={1: b"a" * 32, 2: b"b" * 32},
        hmac_keys={1: b"c" * 32, 2: b"d" * 32},
        active_version=2,
    )
    destination.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {"key_version": 1, "phone_hmac": crypto.phone_hmac(phone, 1)},
                    {"key_version": 2, "phone_hmac": crypto.phone_hmac(phone, 2)},
                ],
            }
        ),
        encoding="utf-8",
    )

    count = module.run_vendor_test_recipient(
        action="remove",
        hidden_input=FakeHiddenTtyInput(phone),
        crypto=crypto,
        destination=destination,
    )

    assert count == 0
    assert json.loads(destination.read_text(encoding="utf-8"))["entries"] == []


def test_vendor_test_recipient_parser_has_no_phone_or_destination_override() -> None:
    module = load_cli_module()
    parser = module.build_parser()

    args = parser.parse_args(["vendor-test-recipient", "add"])
    assert args.action == "add"
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["vendor-test-recipient", "add", "--phone", "13900000001"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["vendor-test-recipient", "add", "--destination", "/tmp/out"]
        )
