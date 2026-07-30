from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import InvalidCredentials
from app.core.auth.users import SqlUserRepository

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

LOGINS = (
    "atomic-password-rollback",
    "atomic-password-concurrent",
    "atomic-password-expired",
    "atomic-password-account-off",
    "atomic-password-identity-off",
    "atomic-password-provider-mismatch",
)


@pytest.mark.asyncio
async def test_password_change_token_rollback_and_concurrency_are_atomic() -> None:
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
    account_ids: list[int] = []
    identity_ids: list[int] = []

    async def create_local(login_name: str) -> tuple[int, int, int]:
        async with engine.begin() as connection:
            provider_id = int(
                (
                    await connection.execute(
                        text("SELECT id FROM auth_provider WHERE code='local'")
                    )
                ).scalar_one()
            )
            account_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(
                              display_name,dept,role,role_override,status
                            ) VALUES(:display_name,'测试部','operator',TRUE,1)
                            RETURNING id
                            """
                        ),
                        {"display_name": login_name},
                    )
                ).scalar_one()
            )
            identity_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO auth_identity(
                              account_id,provider_id,login_name,
                              normalized_login_name,external_subject,status
                            ) VALUES(
                              :account_id,:provider_id,:login_name,
                              :login_name,:external_subject,1
                            ) RETURNING id
                            """
                        ),
                        {
                            "account_id": account_id,
                            "provider_id": provider_id,
                            "login_name": login_name,
                            "external_subject": f"local:{login_name}",
                        },
                    )
                ).scalar_one()
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO local_credential(
                      identity_id,password_hash,must_change_password
                    ) VALUES(:identity_id,'old-hash',TRUE)
                    """
                ),
                {"identity_id": identity_id},
            )
            security_version = int(
                (
                    await connection.execute(
                        text(
                            "SELECT security_version FROM user_account "
                            "WHERE id=:account_id"
                        ),
                        {"account_id": account_id},
                    )
                ).scalar_one()
            )
        account_ids.append(account_id)
        identity_ids.append(identity_id)
        return account_id, identity_id, security_version

    async def issue(
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        login_name: str,
        security_version: int,
        expires_at: datetime | None = None,
    ) -> None:
        await repository.create_password_change_token(
            token_hash=token_hash,
            account_id=account_id,
            identity_id=identity_id,
            provider_code="local",
            login_name=login_name,
            security_version=security_version,
            expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=10),
        )

    async def consume(
        *,
        token_hash: str,
        account_id: int,
        identity_id: int,
        login_name: str,
        actor: str | None = None,
        provider_code: str = "local",
    ) -> None:
        await repository.consume_password_change_and_update(
            token_hash=token_hash,
            account_id=account_id,
            identity_id=identity_id,
            provider_code=provider_code,
            login_name=login_name,
            password_hash="new-hash",
            actor=actor or login_name,
            ip="127.0.0.1",
        )

    try:
        rollback_account, rollback_identity, rollback_version = await create_local(
            LOGINS[0]
        )
        rollback_hash = "a" * 64
        await issue(
            token_hash=rollback_hash,
            account_id=rollback_account,
            identity_id=rollback_identity,
            login_name=LOGINS[0],
            security_version=rollback_version,
        )
        with pytest.raises(DBAPIError):
            await consume(
                token_hash=rollback_hash,
                account_id=rollback_account,
                identity_id=rollback_identity,
                login_name=LOGINS[0],
                actor="x" * 65,
            )
        await consume(
            token_hash=rollback_hash,
            account_id=rollback_account,
            identity_id=rollback_identity,
            login_name=LOGINS[0],
        )

        concurrent_account, concurrent_identity, concurrent_version = await create_local(
            LOGINS[1]
        )
        concurrent_hash = "b" * 64
        await issue(
            token_hash=concurrent_hash,
            account_id=concurrent_account,
            identity_id=concurrent_identity,
            login_name=LOGINS[1],
            security_version=concurrent_version,
        )
        results = await asyncio.gather(
            consume(
                token_hash=concurrent_hash,
                account_id=concurrent_account,
                identity_id=concurrent_identity,
                login_name=LOGINS[1],
            ),
            consume(
                token_hash=concurrent_hash,
                account_id=concurrent_account,
                identity_id=concurrent_identity,
                login_name=LOGINS[1],
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, InvalidCredentials) for result in results) == 1

        rejection_cases: list[tuple[str, int, int, int]] = []
        for login_name in LOGINS[2:]:
            account_id, identity_id, security_version = await create_local(login_name)
            rejection_cases.append(
                (login_name, account_id, identity_id, security_version)
            )

        expired = rejection_cases[0]
        await issue(
            token_hash="c" * 64,
            account_id=expired[1],
            identity_id=expired[2],
            login_name=expired[0],
            security_version=expired[3],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        with pytest.raises(InvalidCredentials):
            await consume(
                token_hash="c" * 64,
                account_id=expired[1],
                identity_id=expired[2],
                login_name=expired[0],
            )

        disabled_account = rejection_cases[1]
        await issue(
            token_hash="d" * 64,
            account_id=disabled_account[1],
            identity_id=disabled_account[2],
            login_name=disabled_account[0],
            security_version=disabled_account[3],
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE user_account SET status=0 WHERE id=:account_id"),
                {"account_id": disabled_account[1]},
            )
        with pytest.raises(InvalidCredentials):
            await consume(
                token_hash="d" * 64,
                account_id=disabled_account[1],
                identity_id=disabled_account[2],
                login_name=disabled_account[0],
            )

        disabled_identity = rejection_cases[2]
        await issue(
            token_hash="e" * 64,
            account_id=disabled_identity[1],
            identity_id=disabled_identity[2],
            login_name=disabled_identity[0],
            security_version=disabled_identity[3],
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_identity SET status=0 WHERE id=:identity_id"),
                {"identity_id": disabled_identity[2]},
            )
        with pytest.raises(InvalidCredentials):
            await consume(
                token_hash="e" * 64,
                account_id=disabled_identity[1],
                identity_id=disabled_identity[2],
                login_name=disabled_identity[0],
            )

        mismatched_provider = rejection_cases[3]
        await issue(
            token_hash="f" * 64,
            account_id=mismatched_provider[1],
            identity_id=mismatched_provider[2],
            login_name=mismatched_provider[0],
            security_version=mismatched_provider[3],
        )
        with pytest.raises(InvalidCredentials):
            await consume(
                token_hash="f" * 64,
                account_id=mismatched_provider[1],
                identity_id=mismatched_provider[2],
                login_name=mismatched_provider[0],
                provider_code="ldap",
            )

        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT pct.status,pct.consumed_at,lc.password_hash,
                               lc.must_change_password,ua.security_version
                        FROM password_change_token pct
                        JOIN user_account ua ON ua.id=pct.account_id
                        JOIN local_credential lc ON lc.identity_id=pct.identity_id
                        WHERE pct.account_id=ANY(CAST(:account_ids AS bigint[]))
                        ORDER BY pct.account_id
                        """
                    ),
                    {"account_ids": [rollback_account, concurrent_account]},
                )
            ).mappings().all()
        assert len(rows) == 2
        assert all(row["status"] == "consumed" for row in rows)
        assert all(row["consumed_at"] is not None for row in rows)
        assert all(row["password_hash"] == "new-hash" for row in rows)
        assert all(row["must_change_password"] is False for row in rows)
        assert [int(row["security_version"]) for row in rows] == [
            rollback_version + 1,
            concurrent_version + 1,
        ]
    finally:
        async with engine.begin() as connection:
            if account_ids:
                await connection.execute(
                    text(
                        "DELETE FROM audit_log "
                        "WHERE actor_account_id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": account_ids},
                )
                await connection.execute(
                    text(
                        "DELETE FROM password_change_token "
                        "WHERE account_id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": account_ids},
                )
            if identity_ids:
                await connection.execute(
                    text(
                        "DELETE FROM local_credential "
                        "WHERE identity_id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": identity_ids},
                )
                await connection.execute(
                    text(
                        "DELETE FROM auth_identity "
                        "WHERE id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": identity_ids},
                )
            if account_ids:
                await connection.execute(
                    text(
                        "DELETE FROM user_account "
                        "WHERE id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": account_ids},
                )
        await engine.dispose()
