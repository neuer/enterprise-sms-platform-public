"""认证源精确路由、Provider kind 注册表与 LDAP/Mock 动态装配。"""

from __future__ import annotations

from typing import Protocol

from app.core.auth.backends import (
    AuthenticatedIdentity,
    ProviderDisabled,
    ProviderUnavailable,
)
from app.core.auth.ldap_real import LdapConfig, LdapPasswordProvider
from app.core.auth.local import LocalAccountReader, LocalPasswordProvider
from app.core.auth.mock import MockLdapProvider
from app.core.auth.passwords import LocalPasswordHasher
from app.services.auth_provider import (
    InvalidProviderConfig,
    LdapProviderConfig,
    ProviderNotFound,
    ProviderRecord,
    ProviderTestResult,
    validate_ldap_allowed,
)
from app.settings import Settings


class ProviderRecordLoader(Protocol):
    async def get(self, code: str) -> ProviderRecord: ...


class ProviderKindHandler(Protocol):
    def auth_flow(self) -> str: ...

    def validate_config(self, config: dict[str, object]) -> dict[str, object]: ...

    async def test_config(
        self,
        config: dict[str, object],
    ) -> ProviderTestResult: ...

    async def authenticate(
        self,
        record: ProviderRecord,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity: ...


class AuthProviderRegistry:
    """每次认证都读取 Provider 当前状态，并只调用精确匹配的一个实现。"""

    def __init__(
        self,
        repository: ProviderRecordLoader,
        handlers: dict[str, ProviderKindHandler],
    ) -> None:
        self.repository = repository
        self.handlers = dict(handlers)

    def _handler(self, kind: str) -> ProviderKindHandler:
        try:
            return self.handlers[kind]
        except KeyError:
            raise ProviderUnavailable("认证源实现暂不可用") from None

    def auth_flow(self, kind: str) -> str:
        return self._handler(kind).auth_flow()

    def validate_config(
        self,
        kind: str,
        config: dict[str, object],
    ) -> dict[str, object]:
        return self._handler(kind).validate_config(config)

    async def test_config(
        self,
        kind: str,
        config: dict[str, object],
    ) -> ProviderTestResult:
        return await self._handler(kind).test_config(config)

    async def authenticate(
        self,
        provider_code: str,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        try:
            record = await self.repository.get(provider_code)
        except (ProviderNotFound, KeyError):
            raise ProviderDisabled("认证源未启用") from None
        if not record.enabled:
            raise ProviderDisabled("认证源未启用")
        if record.kind != "local" and record.active_config is None:
            raise ProviderDisabled("认证源未激活")
        handler = self._handler(record.kind)
        return await handler.authenticate(record, login_name, password)


class LocalProviderKind:
    def __init__(self, provider: LocalPasswordProvider) -> None:
        self.provider = provider

    def auth_flow(self) -> str:
        return "password"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        if config:
            raise InvalidProviderConfig("内置本地认证源不接受配置")
        return {}

    async def test_config(self, config: dict[str, object]) -> ProviderTestResult:
        self.validate_config(config)
        return ProviderTestResult(True, "OK")

    async def authenticate(
        self,
        record: ProviderRecord,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        del record
        return await self.provider.authenticate(login_name, password)


class LdapProviderKind:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def auth_flow(self) -> str:
        return "password"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        value = LdapProviderConfig.from_mapping(config)
        validate_ldap_allowed(value.server, self.settings.ldap_allowed_host_set)
        return value.to_mapping()

    def _provider(
        self,
        provider_code: str,
        config: dict[str, object],
    ) -> LdapPasswordProvider:
        value = LdapProviderConfig.from_mapping(config)
        validate_ldap_allowed(value.server, self.settings.ldap_allowed_host_set)
        return LdapPasswordProvider(
            LdapConfig(
                provider_code=provider_code,
                server=value.server,
                base_dn=value.base_dn,
                bind_dn=value.bind_dn,
                bind_password=self.settings.credential("ldap_bind_password"),
                user_search_filter=value.user_search_filter,
                username_attribute=value.username_attribute,
                display_name_attribute=value.display_name_attribute,
                dept_attribute=value.dept_attribute,
                subject_attribute=value.subject_attribute,
                group_attribute=value.group_attribute,
                ca_certs_file=str(self.settings.ldap_ca_certs_file),
                connect_timeout_s=value.connect_timeout_s,
                receive_timeout_s=value.receive_timeout_s,
            )
        )

    async def test_config(self, config: dict[str, object]) -> ProviderTestResult:
        try:
            await self._provider("ad", config).test_connection()
        except (ProviderUnavailable, RuntimeError):
            return ProviderTestResult(False, "LDAP_CONNECTION_FAILED")
        return ProviderTestResult(True, "OK")

    async def authenticate(
        self,
        record: ProviderRecord,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        if record.active_config is None:
            raise ProviderDisabled("认证源未激活")
        return await self._provider(record.code, record.active_config).authenticate(
            login_name,
            password,
        )


class MockLdapProviderKind:
    """开发模式仅替换 ldap kind，本地 Provider 仍走真实 Argon2。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def auth_flow(self) -> str:
        return "password"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        return LdapProviderConfig.from_mapping(config).to_mapping()

    async def test_config(self, config: dict[str, object]) -> ProviderTestResult:
        self.validate_config(config)
        return ProviderTestResult(True, "MOCK_OK")

    async def authenticate(
        self,
        record: ProviderRecord,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        del record
        return await MockLdapProvider(
            self.settings.credential("ldap_bind_password")
        ).authenticate(login_name, password)


def create_provider_registry(
    *,
    settings: Settings,
    provider_repository: ProviderRecordLoader,
    local_repository: LocalAccountReader,
) -> AuthProviderRegistry:
    """装配同时存在的 local 与 ldap kind；AUTH_MOCK 只替换后者。"""

    ldap: ProviderKindHandler = (
        MockLdapProviderKind(settings) if settings.auth_mock else LdapProviderKind(settings)
    )
    return AuthProviderRegistry(
        provider_repository,
        {
            "local": LocalProviderKind(
                LocalPasswordProvider(local_repository, LocalPasswordHasher())
            ),
            "ldap": ldap,
        },
    )
