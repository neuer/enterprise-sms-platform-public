"""基于 ldap3 的真实 AD 密码 Provider；单测只允许 monkeypatch 网络对象。"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ldap3 import SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPBindError, LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    ProviderUnavailable,
)
from app.core.bounded_executor import ExecutorBackpressure, run_bounded

LDAP_MAX_RESPONSE_BYTES = 256 * 1024
LDAP_MAX_SCALAR_BYTES = 4096
LDAP_MAX_GROUPS = 256
LDAP_MAX_GROUP_BYTES = 1024
LDAP_MAX_GROUP_TOTAL_BYTES = 64 * 1024


class _ReceiveBudgetSocket:
    """在 ldap3 ASN.1 解析前限制一个连接可接收的 LDAP 字节数。"""

    def __init__(self, wrapped: Any, limit: int = LDAP_MAX_RESPONSE_BYTES) -> None:
        self._wrapped = wrapped
        self._remaining = limit

    def recv(self, size: int, *args: object) -> bytes:
        data = bytes(self._wrapped.recv(min(size, self._remaining + 1), *args))
        self._remaining -= len(data)
        if self._remaining < 0:
            raise OSError("LDAP response exceeds byte budget")
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def _bounded_text(value: object, *, field: str) -> str:
    rendered = _stable_subject(value)
    if len(rendered.encode("utf-8")) > LDAP_MAX_SCALAR_BYTES:
        raise ProviderUnavailable(f"LDAP {field} 属性超出限制")
    return rendered


@dataclass(frozen=True, slots=True)
class LdapConfig:
    provider_code: str
    server: str
    base_dn: str
    bind_dn: str
    bind_password: str
    user_search_filter: str
    username_attribute: str
    display_name_attribute: str
    dept_attribute: str
    subject_attribute: str
    group_attribute: str
    ca_certs_file: str
    connect_timeout_s: float
    receive_timeout_s: float


def _attribute_value(entry: object, name: str) -> object:
    attribute = getattr(entry, name, None)
    return getattr(attribute, "value", None)


def _attribute_values(entry: object, name: str) -> tuple[str, ...]:
    attribute = getattr(entry, name, None)
    values = getattr(attribute, "values", ()) or ()
    if len(values) > LDAP_MAX_GROUPS:
        raise ProviderUnavailable("LDAP 组属性超出限制")
    rendered: list[str] = []
    total = 0
    for value in values:
        item = str(value)
        size = len(item.encode("utf-8"))
        if size > LDAP_MAX_GROUP_BYTES:
            raise ProviderUnavailable("LDAP 组属性超出限制")
        total += size
        if total > LDAP_MAX_GROUP_TOTAL_BYTES:
            raise ProviderUnavailable("LDAP 组属性超出限制")
        rendered.append(item)
    return tuple(rendered)


def _stable_subject(value: object) -> str:
    if isinstance(value, bytes):
        return value.hex()
    return str(value or "").strip()


class LdapPasswordProvider:
    """服务绑定检索稳定主体，再以用户密码 bind 验证。"""

    def __init__(self, config: LdapConfig) -> None:
        self.config = config

    async def authenticate(
        self,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        if not login_name or not password:
            raise InvalidCredentials("用户名或密码错误")
        try:
            return await run_bounded(
                self._authenticate_sync,
                login_name,
                password,
                timeout_s=self._authentication_deadline_s,
                pool="ldap",
            )
        except (ExecutorBackpressure, TimeoutError):
            raise ProviderCapacityUnavailable("LDAP 认证容量暂不可用") from None

    async def test_connection(self) -> None:
        """仅验证服务绑定；不接收也不记录任何用户密码。"""

        try:
            await run_bounded(
                self._test_connection_sync,
                timeout_s=self._connection_test_deadline_s,
                pool="ldap",
            )
        except (ExecutorBackpressure, TimeoutError):
            raise ProviderCapacityUnavailable("LDAP 认证容量暂不可用") from None

    @property
    def _authentication_deadline_s(self) -> float:
        """覆盖服务/用户两次连接和三次响应，避免外层先于 ldap3 超时。"""

        return (
            2 * self.config.connect_timeout_s
            + 3 * self.config.receive_timeout_s
            + 1
        )

    @property
    def _connection_test_deadline_s(self) -> float:
        """覆盖服务连接、bind 与一次有界查询。"""

        return self.config.connect_timeout_s + 2 * self.config.receive_timeout_s + 1

    def _server(self) -> Any:
        parsed = urlsplit(self.config.server)
        if parsed.scheme != "ldaps" or not parsed.hostname:
            raise ProviderUnavailable("LDAP 服务暂不可用")
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=self.config.ca_certs_file,
        )
        return Server(
            parsed.hostname,
            port=parsed.port or 636,
            use_ssl=True,
            tls=tls,
            connect_timeout=self.config.connect_timeout_s,
            allowed_referral_hosts=[],
        )

    @staticmethod
    def _open_bounded_connection(server: Any, **kwargs: Any) -> Connection:
        connection = Connection(
            server,
            auto_bind=False,
            auto_referrals=False,
            raise_exceptions=True,
            **kwargs,
        )
        connection.open()
        connection.socket = _ReceiveBudgetSocket(connection.socket)
        connection.bind()
        return connection

    def _service_connection(self, server: Any) -> Connection:
        return self._open_bounded_connection(
            server,
            user=self.config.bind_dn,
            password=self.config.bind_password,
            receive_timeout=self.config.receive_timeout_s,
        )

    def _test_connection_sync(self) -> None:
        service: Connection | None = None
        try:
            service = self._service_connection(self._server())
            searched = service.search(
                search_base=self.config.base_dn,
                search_filter="(objectClass=*)",
                search_scope=SUBTREE,
                attributes=[self.config.subject_attribute],
                size_limit=1,
            )
            if not searched:
                raise ProviderUnavailable("LDAP Base DN 不可用")
        except ProviderUnavailable:
            raise
        except LDAPException:
            raise ProviderUnavailable("LDAP 服务暂不可用") from None
        finally:
            if service is not None:
                service.unbind()

    def _authenticate_sync(
        self,
        login_name: str,
        password: str,
    ) -> AuthenticatedIdentity:
        service: Connection | None = None
        try:
            server = self._server()
            service = self._service_connection(server)
            search_filter = self.config.user_search_filter.format(
                username=escape_filter_chars(login_name)
            )
            attributes = list(
                dict.fromkeys(
                    (
                        self.config.username_attribute,
                        self.config.display_name_attribute,
                        self.config.dept_attribute,
                        self.config.subject_attribute,
                        self.config.group_attribute,
                    )
                )
            )
            service.search(
                search_base=self.config.base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=attributes,
                size_limit=2,
            )
            if len(service.entries) != 1:
                raise InvalidCredentials("用户名或密码错误")
            entry = service.entries[0]
            subject = _bounded_text(
                _attribute_value(entry, self.config.subject_attribute),
                field="subject",
            )
            if not subject:
                raise ProviderUnavailable("LDAP 稳定主体属性不可用")
            try:
                user_connection = self._open_bounded_connection(
                    server,
                    user=entry.entry_dn,
                    password=password,
                    receive_timeout=self.config.receive_timeout_s,
                )
            except LDAPBindError:
                raise InvalidCredentials("用户名或密码错误") from None
            else:
                user_connection.unbind()
            directory_login = _bounded_text(
                _attribute_value(entry, self.config.username_attribute) or login_name,
                field="username",
            )
            return AuthenticatedIdentity(
                provider_code=self.config.provider_code,
                login_name=directory_login,
                external_subject=subject,
                display_name=_bounded_text(
                    _attribute_value(entry, self.config.display_name_attribute) or directory_login,
                    field="display_name",
                ),
                dept=_bounded_text(
                    _attribute_value(entry, self.config.dept_attribute) or "",
                    field="department",
                ),
                groups=_attribute_values(entry, self.config.group_attribute),
            )
        except (InvalidCredentials, ProviderUnavailable):
            raise
        except LDAPException:
            raise ProviderUnavailable("LDAP 服务暂不可用") from None
        finally:
            if service is not None:
                service.unbind()


# 兼容尚未迁移的导入；运行装配只使用 LdapPasswordProvider。
LdapAuthBackend = LdapPasswordProvider
