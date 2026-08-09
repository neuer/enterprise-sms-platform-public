from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials
from app.core.auth.jwt import JwtClaims, JwtService
from app.core.auth.users import SqlUserRepository

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

SECRET = "security-session-postgres-secret-long-enough"
PROVIDER_CODES = ("security-session-ad-a", "security-session-ad-b")
LOGINS = (
    "security-session-primary",
    "security-session-secondary",
    "security-session-other",
)


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    async def get(self, key: str) -> object | None:
        return self.values.get(key)

    async def set(self, key: str, value: object, *, ex: int) -> None:
        del ex
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def increment(self, key: str, *, window_s: int) -> int:
        del window_s
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        del script
        assert numkeys == 2
        key, revoked_session, expected, replacement, _ttl, session_ttl = args
        current = self.values.get(str(key))
        if current is None:
            assert int(session_ttl) > 0
            self.values[str(revoked_session)] = "1"
            self.values.pop(str(key), None)
            return 0
        if current != expected:
            assert int(session_ttl) > 0
            self.values[str(revoked_session)] = "1"
            self.values.pop(str(key), None)
            return -1
        self.values[str(key)] = replacement
        return 1


def claims(projection: Any) -> JwtClaims:
    return JwtClaims(
        account_id=projection.account_id,
        identity_id=projection.identity_id,
        provider_code=projection.provider_code,
        login_name=projection.login_name,
        display_name=projection.display_name,
        dept=projection.dept,
        role=projection.role,
        security_version=projection.security_version,
    )


@pytest.mark.asyncio
async def test_real_postgres_security_projection_invalidates_every_authorization_boundary() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
        ),
    )
    repository = SqlUserRepository(settings)
    engine = create_async_engine(database_url)

    async def assert_version_pair(account_id: int) -> tuple[int, int]:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT auth_version,security_version
                        FROM user_account WHERE id=:account_id
                        """
                    ),
                    {"account_id": account_id},
                )
            ).mappings().one()
        pair = (int(row["auth_version"]), int(row["security_version"]))
        assert pair[0] == pair[1]
        return pair

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE actor_account_id IN (
                      SELECT account_id FROM auth_identity
                      WHERE normalized_login_name=ANY(
                        CAST(:logins AS varchar(64)[])
                      )
                    )
                    """
                ),
                {"logins": list(LOGINS)},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM external_role_mapping
                    WHERE provider_id IN (
                      SELECT id FROM auth_provider
                      WHERE code=ANY(CAST(:provider_codes AS varchar(64)[]))
                    )
                    """
                ),
                {"provider_codes": list(PROVIDER_CODES)},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM auth_identity
                    WHERE normalized_login_name=ANY(CAST(:logins AS varchar(64)[]))
                    """
                ),
                {"logins": list(LOGINS)},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM user_account
                    WHERE display_name LIKE 'security-session-%'
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM auth_provider
                    WHERE code=ANY(CAST(:provider_codes AS varchar(64)[]))
                    """
                ),
                {"provider_codes": list(PROVIDER_CODES)},
            )

    try:
        await cleanup()
        async with engine.begin() as connection:
            provider_ids: list[int] = []
            for code in PROVIDER_CODES:
                provider_ids.append(
                    int(
                        (
                            await connection.execute(
                                text(
                                    """
                                    INSERT INTO auth_provider(
                                      code,name,kind,enabled,active_config,active_version
                                    ) VALUES(
                                      :code,:code,'ldap',TRUE,'{}'::jsonb,1
                                    ) RETURNING id
                                    """
                                ),
                                {"code": code},
                            )
                        ).scalar_one()
                    )
                )
            account_primary = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(
                              display_name,dept,role,role_override
                            ) VALUES(
                              'security-session-primary','平台部','operator',FALSE
                            ) RETURNING id
                            """
                        )
                    )
                ).scalar_one()
            )
            account_other = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(
                              display_name,dept,role,role_override
                            ) VALUES(
                              'security-session-other','财务部','viewer',FALSE
                            ) RETURNING id
                            """
                        )
                    )
                ).scalar_one()
            )
            identity_ids: list[int] = []
            for account_id, provider_id, login in (
                (account_primary, provider_ids[0], LOGINS[0]),
                (account_primary, provider_ids[1], LOGINS[1]),
                (account_other, provider_ids[1], LOGINS[2]),
            ):
                identity_ids.append(
                    int(
                        (
                            await connection.execute(
                                text(
                                    """
                                    INSERT INTO auth_identity(
                                      account_id,provider_id,login_name,
                                      normalized_login_name,external_subject,status
                                    ) VALUES(
                                      :account_id,:provider_id,:login,:login,
                                      :external_subject,1
                                    ) RETURNING id
                                    """
                                ),
                                {
                                    "account_id": account_id,
                                    "provider_id": provider_id,
                                    "login": login,
                                    "external_subject": f"subject:{login}",
                                },
                            )
                        ).scalar_one()
                    )
                )

        synchronized = await repository.resolve_identity(
            AuthenticatedIdentity(
                provider_code=PROVIDER_CODES[0],
                login_name=LOGINS[0],
                external_subject=f"subject:{LOGINS[0]}",
                display_name="security-session-primary",
                dept="平台部",
                groups=("mock:operator",),
            ),
            "127.0.0.1",
        )
        assert synchronized.account_id == account_primary
        assert synchronized.identity_id == identity_ids[0]

        first = await repository.load_security_session(account_primary, identity_ids[0])
        second_identity = await repository.load_security_session(
            account_primary,
            identity_ids[1],
        )
        assert first.active and second_identity.active
        assert first.provider_code == PROVIDER_CODES[0]
        assert second_identity.provider_code == PROVIDER_CODES[1]
        assert await assert_version_pair(account_primary) == (
            first.security_version,
            first.security_version,
        )
        with pytest.raises(InvalidCredentials):
            await repository.load_security_session(account_primary, identity_ids[2])

        # 升级窗口内旧 writer 仍写 auth_version；同步触发器必须把新投影一并推进。
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE user_account
                    SET auth_version=auth_version+1
                    WHERE id=:account_id
                    """
                ),
                {"account_id": account_primary},
            )
        legacy_writer_projection = await repository.load_security_session(
            account_primary,
            identity_ids[0],
        )
        assert legacy_writer_projection.security_version == first.security_version + 1
        assert await assert_version_pair(account_primary) == (
            legacy_writer_projection.security_version,
            legacy_writer_projection.security_version,
        )
        first = legacy_writer_projection

        tokens = JwtService(
            SECRET,
            MemoryStore(),
            security_session_loader=repository.load_security_session,
        )
        token_before_dept_change = tokens.issue(claims(first))
        assert (await tokens.verify(token_before_dept_change)).dept == "平台部"

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE user_account SET dept='安全部' WHERE id=:account_id"),
                {"account_id": account_primary},
            )
        after_dept = await repository.load_security_session(account_primary, identity_ids[0])
        assert after_dept.security_version == first.security_version + 1
        assert after_dept.dept == "安全部"
        with pytest.raises(InvalidCredentials):
            await tokens.verify(token_before_dept_change)

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE user_account SET role='approver' WHERE id=:account_id"),
                {"account_id": account_primary},
            )
        after_role = await repository.load_security_session(account_primary, identity_ids[0])
        assert after_role.security_version == after_dept.security_version + 1
        assert after_role.role == "approver"

        token_before_identity_disable = tokens.issue(claims(after_role))
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_identity SET status=0 WHERE id=:identity_id"),
                {"identity_id": identity_ids[0]},
            )
        disabled_identity = await repository.load_security_session(
            account_primary,
            identity_ids[0],
        )
        assert not disabled_identity.active
        assert disabled_identity.security_version == after_role.security_version + 1
        with pytest.raises(InvalidCredentials):
            await tokens.verify(token_before_identity_disable)

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_identity SET status=1 WHERE id=:identity_id"),
                {"identity_id": identity_ids[0]},
            )
        before_provider_disable = await repository.load_security_session(
            account_primary,
            identity_ids[1],
        )
        secondary_token = tokens.issue(claims(before_provider_disable))
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_provider SET enabled=FALSE WHERE id=:provider_id"),
                {"provider_id": provider_ids[0]},
            )
        disabled_provider = await repository.load_security_session(
            account_primary,
            identity_ids[0],
        )
        assert not disabled_provider.provider_enabled
        assert disabled_provider.security_version == before_provider_disable.security_version + 1
        with pytest.raises(InvalidCredentials):
            await tokens.verify(secondary_token)

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_provider SET enabled=TRUE WHERE id=:provider_id"),
                {"provider_id": provider_ids[0]},
            )
        before_mapping_primary = await repository.load_security_session(
            account_primary,
            identity_ids[0],
        )
        before_mapping_other = await repository.load_security_session(
            account_other,
            identity_ids[2],
        )
        async with engine.begin() as connection:
            mapping_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO external_role_mapping(
                              provider_id,external_group,role
                            ) VALUES(:provider_id,'security-session-group','operator')
                            RETURNING id
                            """
                        ),
                        {"provider_id": provider_ids[0]},
                    )
                ).scalar_one()
            )
        after_mapping_insert = await repository.load_security_session(
            account_primary,
            identity_ids[0],
        )
        assert (
            after_mapping_insert.security_version
            == before_mapping_primary.security_version + 1
        )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE external_role_mapping
                    SET provider_id=:provider_id,role='approver'
                    WHERE id=:mapping_id
                    """
                ),
                {"provider_id": provider_ids[1], "mapping_id": mapping_id},
            )
        after_mapping_move_primary = await repository.load_security_session(
            account_primary,
            identity_ids[0],
        )
        after_mapping_move_other = await repository.load_security_session(
            account_other,
            identity_ids[2],
        )
        assert (
            after_mapping_move_primary.security_version
            == after_mapping_insert.security_version + 1
        )
        assert (
            after_mapping_move_other.security_version
            == before_mapping_other.security_version + 1
        )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE auth_identity
                    SET source_groups=ARRAY['security-session-group']
                    WHERE id=:identity_id
                    """
                ),
                {"identity_id": identity_ids[1]},
            )
        after_groups = await repository.load_security_session(
            account_primary,
            identity_ids[1],
        )
        assert (
            after_groups.security_version
            == after_mapping_move_primary.security_version + 1
        )

        before_mapping_delete_other = await repository.load_security_session(
            account_other,
            identity_ids[2],
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM external_role_mapping WHERE id=:mapping_id"),
                {"mapping_id": mapping_id},
            )
        after_mapping_delete = await repository.load_security_session(
            account_primary,
            identity_ids[1],
        )
        after_mapping_delete_other = await repository.load_security_session(
            account_other,
            identity_ids[2],
        )
        assert after_mapping_delete.security_version == after_groups.security_version + 1
        assert (
            after_mapping_delete_other.security_version
            == before_mapping_delete_other.security_version + 1
        )

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE user_account SET status=0 WHERE id=:account_id"),
                {"account_id": account_primary},
            )
        disabled_account = await repository.load_security_session(
            account_primary,
            identity_ids[1],
        )
        assert not disabled_account.active
        assert (
            disabled_account.security_version
            == after_mapping_delete.security_version + 1
        )
        assert await assert_version_pair(account_primary) == (
            disabled_account.security_version,
            disabled_account.security_version,
        )
    finally:
        await cleanup()
        await engine.dispose()
