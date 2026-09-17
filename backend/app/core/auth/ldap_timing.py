"""部署拥有的 LDAP 安全第二阶段；普通 Provider 配置不得选择目标。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ldap3.utils.dn import parse_dn
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.auth.backends import ProviderUnavailable


def _dn(value: str) -> tuple[tuple[str, str], ...]:
    return tuple((str(key).casefold(), str(part).casefold()) for key, part, _ in parse_dn(value))


def _origin(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "ldaps"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid LDAP timing origin")
    return parsed.hostname.casefold(), parsed.port or 636


class LdapTimingProfile(BaseModel):
    """目录方批准的非业务账号拒绝目标；不保存或重用候选密码。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    server: str = Field(min_length=1, max_length=512, repr=False)
    service_bind_dn: str = Field(min_length=1, max_length=512, repr=False)
    sink_dn: str = Field(min_length=1, max_length=512, repr=False)
    # 这是部署方的安全合同，不是由 API/代码推断的目录对象属性。
    safety_contract: Literal["non_account_rejection"]
    approval_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    expires_at: datetime

    @model_validator(mode="after")
    def validate_target(self) -> LdapTimingProfile:
        _origin(self.server)
        if not _dn(self.sink_dn) or _dn(self.sink_dn) == _dn(self.service_bind_dn):
            raise ValueError("invalid LDAP timing target")
        if self.expires_at.tzinfo is None:
            raise ValueError("LDAP timing expiry must include timezone")
        return self

    def require_current(self, server: str, bind_dn: str) -> None:
        """实际认证再核对目标与到期时间，失效时整条 AD 路径失败关闭。"""

        try:
            valid = (
                _origin(server) == _origin(self.server)
                and _dn(bind_dn) == _dn(self.service_bind_dn)
                and datetime.now(UTC) < self.expires_at
            )
        except Exception:
            valid = False
        if not valid:
            raise ProviderUnavailable("LDAP 安全第二阶段配置不可用") from None


def load_ldap_timing_profile(path: Path | None) -> LdapTimingProfile:
    """只读取受控挂载文件；错误中不回显 DN、地址、文件路径或内容。"""

    try:
        if path is None:
            raise ValueError("missing profile")
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("oversized profile")
        return LdapTimingProfile.model_validate(json.loads(raw))
    except Exception:
        raise ProviderUnavailable("LDAP 安全第二阶段配置不可用") from None
