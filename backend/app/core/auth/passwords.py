"""本地密码策略、Argon2id 哈希与临时密码生成。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.auth.identity import normalize_login_name

PASSWORD_DESCRIPTION: Final = (
    "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名"
)
LOWERCASE: Final = "abcdefghijkmnopqrstuvwxyz"
UPPERCASE: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS: Final = "23456789"
SPECIALS: Final = "!@#$%^&*()-_=+"
_DUMMY_PASSWORD_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=4$3Ls1C++JucbXEeHzOBtakg$"
    "7jbN7KPOdNGfphlj0FdpiRxcUnnIMjaMs26GBxdcj7M"
)


class PasswordPolicyViolation(ValueError):
    """密码不符合平台安全策略。"""


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """服务端权威密码规则，并提供前端只读展示契约。"""

    min_length: int = 12
    max_length: int = 128
    required_character_classes: int = 3

    def validate(self, password: str, *, username: str) -> None:
        if not self.min_length <= len(password) <= self.max_length:
            raise PasswordPolicyViolation("密码长度必须为 12–128 位")
        character_classes = sum(
            (
                any(character.islower() for character in password),
                any(character.isupper() for character in password),
                any(character.isdigit() for character in password),
                any(not character.isalnum() for character in password),
            )
        )
        if character_classes < self.required_character_classes:
            raise PasswordPolicyViolation(
                "密码必须满足至少三类：大写字母、小写字母、数字、特殊字符"
            )
        normalized_username = normalize_login_name(username)
        if normalized_username and normalized_username in password.casefold():
            raise PasswordPolicyViolation("密码不能包含用户名")

    def public_contract(self) -> dict[str, int | bool | str]:
        return {
            "min_length": self.min_length,
            "max_length": self.max_length,
            "required_character_classes": self.required_character_classes,
            "forbid_username": True,
            "description": PASSWORD_DESCRIPTION,
        }


class LocalPasswordHasher:
    """固定成本 Argon2id 封装；账号缺失时仍执行同等级哈希校验。"""

    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        # 固定公开哈希只用于统一缺失账号的校验成本；构造器不得执行昂贵 Argon2。
        self._dummy_hash = _DUMMY_PASSWORD_HASH

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return bool(self._hasher.verify(encoded, password))
        except InvalidHashError:
            # 损坏摘要也执行固定公开摘要，避免异常旁路与显著成本差异。
            self.verify(self._dummy_hash, password)
            return False
        except VerificationError:
            return False

    def verify_or_dummy(self, encoded: str | None, password: str) -> bool:
        if encoded is None:
            self.verify(self._dummy_hash, password)
            return False
        try:
            return bool(self._hasher.verify(encoded, password))
        except InvalidHashError:
            self.verify(self._dummy_hash, password)
            return False
        except VerificationError:
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            return True


def generate_temporary_password(
    *,
    username: str,
    length: int = 20,
    policy: PasswordPolicy | None = None,
) -> str:
    """生成满足全部四类字符且不包含用户名的一次性临时密码。"""

    active_policy = policy or PasswordPolicy()
    if length < max(4, active_policy.min_length) or length > active_policy.max_length:
        raise ValueError("temporary password length violates password policy")
    alphabet = LOWERCASE + UPPERCASE + DIGITS + SPECIALS
    random = secrets.SystemRandom()
    while True:
        characters = [
            secrets.choice(LOWERCASE),
            secrets.choice(UPPERCASE),
            secrets.choice(DIGITS),
            secrets.choice(SPECIALS),
        ]
        characters.extend(secrets.choice(alphabet) for _ in range(length - len(characters)))
        random.shuffle(characters)
        candidate = "".join(characters)
        try:
            active_policy.validate(candidate, username=username)
        except PasswordPolicyViolation:
            continue
        return candidate
