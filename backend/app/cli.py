"""开发与运维命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.auth.identity import validate_local_login_name
from app.core.auth.passwords import (
    LocalPasswordHasher,
    PasswordPolicy,
    generate_temporary_password,
)
from app.core.bounded_executor import run_bounded
from app.core.runtime_resources import bind_connection_audit_subject
from app.services.crypto import (
    PHONE_PATTERN,
    CryptoService,
    EncryptionContext,
    get_crypto_service,
)
from app.services.usage_ledger import UsageLedgerService
from app.services.vendor_event_audit import SqlVendorEventAuditRepository
from app.services.vendor_test_guard import (
    VendorTestAllowlistError,
    VendorTestRecipient,
    read_vendor_test_recipients,
    write_vendor_test_recipients,
)
from app.settings import Settings, get_settings

VENDOR_TEST_ALLOWLIST_WRITE_PATH = Path("/run/vendor-test-rw/allowlist.json")
BACKEND_RUNTIME_GID = 10001


class AuthMockSettings(Protocol):
    """seed-dev 所需的最小配置接口。"""

    auth_mock: bool


@dataclass(frozen=True)
class DevUser:
    username: str
    display_name: str
    dept: str
    role: str


@dataclass(frozen=True)
class DevApp:
    name: str
    dept: str
    allowed_categories: str
    rate_limit_per_min: int = 10_000


@dataclass(frozen=True)
class DevTemplate:
    name: str
    content: str
    var_specs: tuple[dict[str, int], ...]
    dept: str
    vendor_state: str


@dataclass(frozen=True)
class DevSign:
    name: str
    vendor_state: str


DEV_USERS = (
    DevUser("admin01", "开发管理员", "平台技术部", "admin"),
    DevUser("approver01", "开发审批员", "业务一部", "approver"),
    DevUser("operator01", "开发操作员", "业务一部", "operator"),
    DevUser("viewer01", "开发查看员", "业务一部", "viewer"),
)

DEV_APPS = (
    DevApp("app-iam", "平台技术部", "verify"),
    DevApp("app-oa", "业务一部", "notice"),
    DevApp("app-mkt", "市场部", "market"),
)

DEV_TEMPLATE = DevTemplate(
    name="开发验证码模板",
    content="您的验证码是{1}，5分钟内有效。",
    var_specs=({"pos": 1, "max_len": 8},),
    dept="平台技术部",
    vendor_state="approved",
)
DEV_SIGN = DevSign(name="青鸾平台", vendor_state="approved")


class InitAdminError(RuntimeError):
    """初始化管理员失败且可安全展示给操作者。"""


class InitAdminAlreadyCompleted(InitAdminError):
    """系统中已经存在平台账号。"""


class TemporaryPasswordDisplayRequired(InitAdminError):
    """未显式确认临时密码只显示一次。"""


class ControllingTtyRequired(InitAdminError):
    """当前进程没有可安全显示临时密码的控制终端。"""


class InitAdminRepository(Protocol):
    async def create_initial_admin(
        self,
        *,
        username: str,
        normalized_login_name: str,
        display_name: str,
        password_hash: str,
    ) -> None: ...


class TemporaryPasswordOutput(Protocol):
    @property
    def is_tty(self) -> bool: ...

    def write_line(self, value: str) -> None: ...


class HiddenTtyInput(Protocol):
    """只能从控制 TTY 隐藏读取敏感输入的最小接口。"""

    @property
    def is_tty(self) -> bool: ...

    def read_hidden(self, prompt: str) -> str: ...


class StreamTtyOutput:
    """向当前控制 TTY 写一行，不复用 stdout 或日志。"""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    @property
    def is_tty(self) -> bool:
        return self.stream.isatty()

    def write_line(self, value: str) -> None:
        self.stream.write(f"{value}\n")
        self.stream.flush()


class GetpassTtyInput:
    """使用 getpass 从控制 TTY 读取且不回显输入。"""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    @property
    def is_tty(self) -> bool:
        return self.stream.isatty()

    def read_hidden(self, prompt: str) -> str:
        return getpass.getpass(prompt, stream=self.stream)


def run_vendor_test_recipient(
    *,
    action: Literal["add", "remove"],
    hidden_input: HiddenTtyInput,
    crypto: CryptoService,
    destination: Path,
    destination_mode: int = 0o600,
    destination_group_id: int | None = None,
) -> int:
    """从 TTY 读取号码并只把版本化 HMAC 原子写入白名单。"""

    if not hidden_input.is_tty:
        raise ControllingTtyRequired("测试手机号只能从控制 TTY 输入")
    phone = hidden_input.read_hidden("测试手机号：")
    if PHONE_PATTERN.fullmatch(phone) is None:
        raise ValueError("测试手机号格式无效")
    entries = (
        set(read_vendor_test_recipients(destination)) if destination.exists() else set()
    )
    if action == "add":
        entries.add(
            VendorTestRecipient(
                key_version=crypto.active_version,
                phone_hmac=crypto.phone_hmac(phone),
            )
        )
    else:
        entries.difference_update(
            VendorTestRecipient(key_version=version, phone_hmac=digest)
            for version, digest in crypto.hmac_candidates(phone).items()
        )
    write_vendor_test_recipients(
        destination,
        sorted(entries),
        file_mode=destination_mode,
        group_id=destination_group_id,
    )
    return len(entries)


class SqlInitAdminRepository:
    """以事务级 advisory lock 保证空系统初始化只有一个赢家。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_initial_admin(
        self,
        *,
        username: str,
        normalized_login_name: str,
        display_name: str,
        password_hash: str,
    ) -> None:
        engine = create_async_engine(
            self.settings.database_url_for("auth"),
            hide_parameters=True,
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('sms-platform:init-admin'))")
                )
                count_result = await connection.execute(text("SELECT count(*) FROM user_account"))
                if int(count_result.scalar_one()) != 0:
                    raise InitAdminAlreadyCompleted("系统已完成管理员初始化")
                provider_result = await connection.execute(
                    text(
                        """
                        SELECT id FROM auth_provider
                        WHERE code='local' AND enabled=TRUE
                        FOR SHARE
                        """
                    )
                )
                provider_id = provider_result.scalar_one_or_none()
                if provider_id is None:
                    raise InitAdminError("内置本地认证源不可用")
                account_result = await connection.execute(
                    text(
                        """
                        INSERT INTO user_account(
                          display_name,dept,role,role_override,status,security_version
                        ) VALUES(
                          :display_name,'','admin',TRUE,1,1
                        ) RETURNING id
                        """
                    ),
                    {"display_name": display_name},
                )
                account_id = int(account_result.scalar_one())
                identity_result = await connection.execute(
                    text(
                        """
                        INSERT INTO auth_identity(
                          account_id,provider_id,login_name,normalized_login_name,
                          external_subject,status
                        ) VALUES(
                          :account_id,:provider_id,:username,:normalized_login_name,
                          :external_subject,1
                        ) RETURNING id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "provider_id": int(provider_id),
                        "username": username,
                        "normalized_login_name": normalized_login_name,
                        "external_subject": f"local:{normalized_login_name}",
                    },
                )
                identity_id = int(identity_result.scalar_one())
                await connection.execute(
                    text(
                        """
                        INSERT INTO local_credential(
                          identity_id,password_hash,must_change_password
                        ) VALUES(:identity_id,:password_hash,TRUE)
                        """
                    ),
                    {"identity_id": identity_id, "password_hash": password_hash},
                )
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=username,
                    account_id=account_id,
                    identity_id=identity_id,
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'human',:account_id,:identity_id,
                          'admin','initial_local_admin_create',
                          'user_account',:object_id,
                          jsonb_build_object(
                            'provider_code','local',
                            'username',CAST(:audit_username AS text),
                            'credential_change_required',TRUE
                          )
                        )
                        """
                    ),
                    {
                        "actor": username,
                        "account_id": account_id,
                        "identity_id": identity_id,
                        "audit_username": username,
                        "object_id": str(account_id),
                    },
                )
        finally:
            await engine.dispose()


class InitAdminService:
    """验证初始管理员输入，生成 Argon2id 凭据并提交原子初始化。"""

    def __init__(
        self,
        repository: InitAdminRepository,
        *,
        hasher: LocalPasswordHasher | None = None,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.hasher = hasher or LocalPasswordHasher()
        self.policy = policy or PasswordPolicy()

    async def initialize(
        self,
        *,
        username: str,
        display_name: str,
    ) -> str:
        normalized = validate_local_login_name(username)
        normalized_display_name = display_name.strip()
        if not normalized_display_name or len(normalized_display_name) > 128:
            raise InitAdminError("管理员显示名称不能为空且不得超过 128 字符")
        temporary_password = generate_temporary_password(
            username=normalized,
            length=20,
            policy=self.policy,
        )
        password_hash = await run_bounded(
            self.hasher.hash,
            temporary_password,
            timeout_s=5,
        )
        await self.repository.create_initial_admin(
            username=normalized,
            normalized_login_name=normalized,
            display_name=normalized_display_name,
            password_hash=password_hash,
        )
        return temporary_password


async def run_init_admin(
    service: InitAdminService,
    *,
    username: str,
    display_name: str,
    show_temporary_password: bool,
    output: TemporaryPasswordOutput,
) -> str:
    """提交成功后，仅向控制 TTY 显示一次生成的临时密码。"""

    if not show_temporary_password:
        raise TemporaryPasswordDisplayRequired("必须显式传入 --show-temporary-password")
    if not output.is_tty:
        raise ControllingTtyRequired("临时密码只能显示在当前控制 TTY")
    password = await service.initialize(username=username, display_name=display_name)
    output.write_line(f"临时密码（仅显示一次）：{password}")
    return password


def ensure_seed_allowed(settings: AuthMockSettings) -> None:
    """只允许认证 mock 环境写入固定开发身份。"""

    if not settings.auth_mock:
        raise RuntimeError("seed-dev requires AUTH_MOCK=1")


def generate_dev_api_keys() -> dict[str, str]:
    """为三个固定开发应用生成不可预测且互不相同的 API Key。"""

    return {dev_app.name: secrets.token_urlsafe(32) for dev_app in DEV_APPS}


def validate_dev_api_keys(value: object) -> dict[str, str]:
    """校验本地密钥文件结构，拒绝弱值与仓库旧式 ``dev_`` 固定值。"""

    expected_names = {dev_app.name for dev_app in DEV_APPS}
    if not isinstance(value, dict) or set(value) != expected_names:
        raise ValueError("development API key file has an invalid application set")
    keys: dict[str, str] = {}
    for name in sorted(expected_names):
        key = value.get(name)
        if (
            not isinstance(key, str)
            or len(key) < 32
            or any(character.isspace() for character in key)
        ):
            raise ValueError("development API key file contains an invalid key")
        if key.startswith("dev_"):
            raise ValueError("development API key file contains a legacy fixed key")
        keys[name] = key
    if len(set(keys.values())) != len(keys):
        raise ValueError("development API keys must be unique")
    return keys


def load_or_generate_dev_api_keys(source: Path) -> tuple[dict[str, str], bool]:
    """复用安全的本地密钥；缺失或命中旧固定格式时生成新值。"""

    if not source.exists():
        return generate_dev_api_keys(), True
    try:
        if source.stat().st_mode & 0o077:
            raise ValueError("development API key file permissions must be 0600")
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("development API key file is unavailable or invalid") from error
    try:
        return validate_dev_api_keys(value), False
    except ValueError as error:
        if "legacy fixed key" not in str(error):
            raise
        return generate_dev_api_keys(), True


def seed_commands(api_keys: Mapping[str, str]) -> tuple[tuple[str, dict[str, Any]], ...]:
    """生成幂等 seed SQL；数据库参数不包含任何明文 API Key。"""

    validated_api_keys = validate_dev_api_keys(dict(api_keys))
    commands: list[tuple[str, dict[str, Any]]] = []
    commands.append(
        (
            """
            UPDATE auth_provider
            SET enabled=TRUE,
                active_config='{}'::jsonb,
                tested_version=draft_version,
                active_version=draft_version,
                last_tested_at=now(),
                last_test_status='success',
                updated_at=now()
            WHERE code=:provider_code AND kind='ldap'
            """,
            {"provider_code": "ad"},
        )
    )
    commands.append(
        (
            """
            WITH provider AS (
              SELECT id FROM auth_provider WHERE code='ad' AND kind='ldap'
            ), mappings(external_group,role,dept) AS (
              VALUES
                ('mock:admin','admin','平台技术部'),
                ('mock:approver','approver','业务一部'),
                ('mock:operator','operator','业务一部'),
                ('mock:viewer','viewer','业务一部')
            )
            INSERT INTO external_role_mapping(provider_id,external_group,role,dept)
            SELECT p.id,m.external_group,m.role,m.dept
            FROM provider p CROSS JOIN mappings m
            ON CONFLICT (provider_id,external_group) DO UPDATE
            SET role=EXCLUDED.role,dept=EXCLUDED.dept
            """,
            {},
        )
    )
    user_sql = """
        WITH provider AS (
          SELECT id FROM auth_provider WHERE code='ad' AND kind='ldap'
        ), existing AS (
          SELECT ai.account_id
          FROM auth_identity ai
          JOIN provider p ON p.id=ai.provider_id
          WHERE ai.external_subject=:external_subject
        ), created AS (
          INSERT INTO user_account(
            display_name,dept,role,role_override,status,security_version
          )
          SELECT :display_name,:dept,:role,FALSE,1,1
          WHERE NOT EXISTS (SELECT 1 FROM existing)
          RETURNING id
        ), target AS (
          SELECT account_id FROM existing
          UNION ALL
          SELECT id FROM created
        ), updated_account AS (
          UPDATE user_account ua
          SET display_name=:display_name,
              dept=:dept,
              role=:role,
              role_override=FALSE,
              status=1,
              updated_at=now()
          FROM target
          WHERE ua.id=target.account_id
          RETURNING ua.id
        )
        INSERT INTO auth_identity(
          account_id,provider_id,login_name,normalized_login_name,
          external_subject,status,source_groups,last_synced_at
        )
        SELECT ua.id,p.id,:username,:username,:external_subject,1,
               ARRAY[CAST(:source_group AS text)],now()
        FROM updated_account ua CROSS JOIN provider p
        ON CONFLICT (provider_id,external_subject) DO UPDATE SET
          login_name=EXCLUDED.login_name,
          normalized_login_name=EXCLUDED.normalized_login_name,
          status=1,
          source_groups=EXCLUDED.source_groups,
          last_synced_at=now(),
          updated_at=now()
    """
    for user in DEV_USERS:
        commands.append(
            (
                user_sql,
                {
                    **vars(user),
                    "external_subject": f"mock:{user.username}",
                    "source_group": f"mock:{user.role}",
                },
            )
        )

    app_sql = """
        INSERT INTO app (
          name, dept, api_key_hash, api_key_prefix, allowed_categories,
          rate_limit_per_min, created_by, status
        ) VALUES (
          :name, :dept, :api_key_hash, :api_key_prefix, :allowed_categories,
          :rate_limit_per_min, 'seed-dev', 1
        )
        ON CONFLICT (name) DO UPDATE SET
          dept = EXCLUDED.dept,
          api_key_hash = EXCLUDED.api_key_hash,
          api_key_prefix = EXCLUDED.api_key_prefix,
          allowed_categories = EXCLUDED.allowed_categories,
          rate_limit_per_min = EXCLUDED.rate_limit_per_min,
          status = 1,
          updated_at = now()
    """
    for dev_app in DEV_APPS:
        api_key = validated_api_keys[dev_app.name]
        commands.append(
            (
                app_sql,
                {
                    "name": dev_app.name,
                    "dept": dev_app.dept,
                    "api_key_hash": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
                    "api_key_prefix": api_key[:8],
                    "allowed_categories": dev_app.allowed_categories,
                    "rate_limit_per_min": dev_app.rate_limit_per_min,
                },
            )
        )

    commands.append(
        (
            """
            INSERT INTO sms_sign (name, vendor_state, created_by)
            VALUES (:name, :vendor_state, 'seed-dev')
            ON CONFLICT (name) DO UPDATE SET vendor_state = EXCLUDED.vendor_state
            """,
            {"name": DEV_SIGN.name, "vendor_state": DEV_SIGN.vendor_state},
        )
    )
    return tuple(commands)


async def seed_dev_template(
    connection: AsyncConnection,
    crypto: CryptoService,
) -> None:
    """使用模板稳定 ID 绑定密文；seed 参数与 SQL 均不得出现正文。"""

    existing_id = await connection.scalar(
        text(
            """
            SELECT id FROM sms_template
            WHERE created_by='seed-dev'
              AND dept=CAST(:dept AS varchar(128))
            """
        ),
        {"dept": DEV_TEMPLATE.dept},
    )
    if existing_id is not None:
        return
    template_id = int(await connection.scalar(text("SELECT nextval('sms_template_id_seq')")))
    content_enc = crypto.encrypt_bound_packed_text(
        DEV_TEMPLATE.content,
        EncryptionContext(
            domain="sms-template-content",
            table="sms_template",
            column="content_enc",
            object_id=str(template_id),
        ),
    )
    name_enc = crypto.encrypt_bound_packed_text(
        DEV_TEMPLATE.name,
        EncryptionContext(
            domain="sms-template-name",
            table="sms_template",
            column="name_enc",
            object_id=str(template_id),
        ),
    )
    await connection.execute(
        text(
            """
            INSERT INTO sms_template(
              id,name,name_enc,content,content_enc,var_specs,dept,vendor_state,created_by
            ) VALUES(
              :id,'[encrypted]',:name_enc,'[encrypted]',:content_enc,
              CAST(:var_specs AS jsonb),CAST(:dept AS varchar(128)),
              CAST(:vendor_state AS varchar(10)),'seed-dev'
            )
            """
        ),
        {
            "id": template_id,
            "name_enc": name_enc,
            "content_enc": content_enc,
            "var_specs": json.dumps(DEV_TEMPLATE.var_specs, ensure_ascii=False),
            "dept": DEV_TEMPLATE.dept,
            "vendor_state": DEV_TEMPLATE.vendor_state,
        },
    )


async def seed_database(
    engine: AsyncEngine,
    api_keys: Mapping[str, str],
    crypto: CryptoService,
) -> None:
    """在单个事务内幂等写入全部开发数据。"""

    async with engine.begin() as connection:
        for sql, params in seed_commands(api_keys):
            await connection.execute(text(sql), params)
        await seed_dev_template(connection, crypto)


def write_dev_api_keys(destination: Path, api_keys: Mapping[str, str]) -> None:
    """以原子替换和 0600 权限写入随机开发 API Key。"""

    payload = validate_dev_api_keys(dict(api_keys))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


async def run_seed_dev(settings: Settings, keys_file: Path) -> None:
    """校验环境、写数据库，并仅在成功后落开发密钥文件。"""

    ensure_seed_allowed(settings)
    api_keys, _generated = load_or_generate_dev_api_keys(keys_file)
    auth_engine = create_async_engine(
        settings.database_url_for("auth"),
        hide_parameters=True,
    )
    accept_engine = create_async_engine(
        settings.database_url_for("accept"),
        hide_parameters=True,
    )
    try:
        commands = seed_commands(api_keys)
        crypto = CryptoService.from_settings(settings)
        auth_command_count = 2 + len(DEV_USERS)
        async with auth_engine.begin() as connection:
            for sql, params in commands[:auth_command_count]:
                await connection.execute(text(sql), params)
        async with accept_engine.begin() as connection:
            for sql, params in commands[auth_command_count:]:
                await connection.execute(text(sql), params)
            await seed_dev_template(connection, crypto)
    finally:
        await auth_engine.dispose()
        await accept_engine.dispose()
    write_dev_api_keys(keys_file, api_keys)


async def run_usage_projection_rebuild(settings: Settings) -> int:
    """从 PostgreSQL 事实安全覆盖 Redis 投影。"""

    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        return await UsageLedgerService(
            redis,
            settings,
            pooled=False,
        ).rebuild(actor="operator:usage-projection-cli")
    finally:
        await redis.aclose()


async def run_usage_ledger_explain(
    settings: Settings,
    *,
    reservation_id: UUID | None,
    batch_no: str | None,
) -> dict[str, Any]:
    """返回无手机号/HMAC 的配额与频控计数解释。"""

    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        return await UsageLedgerService(
            redis,
            settings,
            pooled=False,
        ).explain(reservation_id=reservation_id, batch_no=batch_no)
    finally:
        await redis.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)
    seed_parser = subcommands.add_parser("seed-dev", help="写入 mock 用户与随机密钥应用")
    seed_parser.add_argument(
        "--keys-file",
        type=Path,
        default=Path("deploy/secrets/dev-apikeys.txt"),
    )
    init_parser = subcommands.add_parser(
        "init-admin",
        help="仅空系统创建首个本地管理员",
    )
    init_parser.add_argument("--username", default="admin")
    init_parser.add_argument("--display-name", default="系统管理员")
    init_parser.add_argument("--show-temporary-password", action="store_true")
    recipient_parser = subcommands.add_parser(
        "vendor-test-recipient",
        help="通过控制 TTY 维护真实联调测试号码",
    )
    recipient_parser.add_argument("action", choices=("add", "remove"))
    subcommands.add_parser(
        "usage-projection-rebuild",
        help="从 PostgreSQL 事实账本重建配额与频控 Redis 投影",
    )
    explain_parser = subcommands.add_parser(
        "usage-ledger-explain",
        help="以无 PII 维度解释某次配额与频控计数",
    )
    reference = explain_parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--reservation-id", type=UUID)
    reference.add_argument("--batch-no")
    subcommands.add_parser(
        "vendor-event-duplicate-audit",
        help="只读检测升级前跨密钥版本的重复回复投影",
    )
    return parser


def main() -> int:
    """执行命令且不向输出打印任何明文 Key。"""

    args = build_parser().parse_args()
    if args.command == "seed-dev":
        asyncio.run(run_seed_dev(get_settings(), args.keys_file))
        print(f"seed-dev 完成: users={len(DEV_USERS)} apps={len(DEV_APPS)}")
        return 0
    if args.command == "init-admin":
        try:
            with Path("/dev/tty").open("w", encoding="utf-8") as stream:
                asyncio.run(
                    run_init_admin(
                        InitAdminService(SqlInitAdminRepository(get_settings())),
                        username=args.username,
                        display_name=args.display_name,
                        show_temporary_password=args.show_temporary_password,
                        output=StreamTtyOutput(stream),
                    )
                )
        except OSError:
            print("init-admin 失败：临时密码只能显示在当前控制 TTY", file=sys.stderr)
            return 1
        except (InitAdminError, ValueError) as error:
            print(f"init-admin 失败：{error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "vendor-test-recipient":
        try:
            with Path("/dev/tty").open("r+", encoding="utf-8") as stream:
                count = run_vendor_test_recipient(
                    action=args.action,
                    hidden_input=GetpassTtyInput(stream),
                    crypto=get_crypto_service(),
                    destination=VENDOR_TEST_ALLOWLIST_WRITE_PATH,
                    destination_mode=0o640,
                    destination_group_id=BACKEND_RUNTIME_GID,
                )
        except OSError:
            print("vendor-test-recipient 失败：测试手机号只能从控制 TTY 输入", file=sys.stderr)
            return 1
        except (ControllingTtyRequired, VendorTestAllowlistError, ValueError) as error:
            print(f"vendor-test-recipient 失败：{error}", file=sys.stderr)
            return 1
        print(f"vendor-test-recipient 完成: entries={count}")
        return 0
    if args.command == "usage-projection-rebuild":
        count = asyncio.run(run_usage_projection_rebuild(get_settings()))
        print(f"usage-projection-rebuild 完成: dimensions={count}")
        return 0
    if args.command == "usage-ledger-explain":
        result = asyncio.run(
            run_usage_ledger_explain(
                get_settings(),
                reservation_id=args.reservation_id,
                batch_no=args.batch_no,
            )
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "vendor-event-duplicate-audit":
        groups = asyncio.run(
            SqlVendorEventAuditRepository(get_settings()).duplicate_reply_groups(
                get_crypto_service()
            )
        )
        print(
            json.dumps(
                {
                    "duplicate_groups": [group.safe_json() for group in groups],
                    "group_count": len(groups),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
