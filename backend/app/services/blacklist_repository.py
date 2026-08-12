"""黑名单 PostgreSQL 事实源；审计只记录数量，不记录号码或号码派生列表。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import bind_connection_audit_subject, database_engine
from app.services.blacklist import BlacklistEntry, BlacklistPage, BlacklistUpsertResult
from app.settings import Settings, get_settings


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，配合 ESCAPE '\\' 使用。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def upsert_blacklist_entries(
    connection: Any,
    entries: list[BlacklistEntry],
    *,
    principal: SecurityPrincipal,
    ip: str,
    source: str,
    audit_action: str = "blacklist_add",
    audit_object_id: str = "batch",
) -> BlacklistUpsertResult:
    """按全版本 HMAC 别名合并逻辑号码，并在同事务写审计。"""

    digests = sorted(
        {
            digest
            for entry in entries
            for _version, digest in (
                entry.hmac_candidates or ((entry.key_version, entry.phone_hmac),)
            )
        }
    )
    resolved = await connection.execute(
        text(
            """
            SELECT hmac_digest,blacklist_digest
            FROM blacklist_hmac_alias
            WHERE hmac_digest=ANY(CAST(:digests AS char(64)[]))
            UNION ALL
            SELECT phone_hmac,phone_hmac FROM blacklist
            WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
            """
        ),
        {"digests": digests},
    )
    owners_by_digest: dict[str, set[str]] = {}
    for row in resolved.mappings():
        owners_by_digest.setdefault(str(row["hmac_digest"]).strip(), set()).add(
            str(row["blacklist_digest"]).strip()
        )

    existing_rows: list[dict[str, object]] = []
    new_rows: list[dict[str, object]] = []
    canonical_aliases: list[tuple[str, tuple[tuple[int, str], ...]]] = []
    duplicate_owners: set[str] = set()
    updated = 0
    for entry in entries:
        aliases = entry.hmac_candidates or ((entry.key_version, entry.phone_hmac),)
        owners = {
            owner
            for _version, digest in aliases
            for owner in owners_by_digest.get(digest, set())
        }
        if owners:
            updated += 1
            current = entry.phone_hmac if entry.phone_hmac in owners else sorted(owners)[0]
            duplicate_owners.update(owners - {current})
        else:
            current = None
        row = {
            "current": current,
            "canonical": entry.phone_hmac,
            "phone_enc": entry.phone_enc,
            "phone_mask": entry.phone_mask,
            "key_version": entry.key_version,
            "source": source,
            "remark": entry.remark,
            "actor": principal.login_name,
        }
        (existing_rows if current is not None else new_rows).append(row)
        canonical_aliases.append((entry.phone_hmac, aliases))

    if duplicate_owners:
        await connection.execute(
            text("DELETE FROM blacklist WHERE phone_hmac=ANY(CAST(:owners AS char(64)[]))"),
            {"owners": sorted(duplicate_owners)},
        )
    if existing_rows:
        await connection.execute(
            text(
                """
                UPDATE blacklist SET
                  phone_hmac=:canonical,phone_enc=:phone_enc,phone_mask=:phone_mask,
                  key_version=:key_version,source=:source,remark=:remark
                WHERE phone_hmac=:current
                """
            ),
            existing_rows,
        )
    if new_rows:
        await connection.execute(
            text(
                """
                INSERT INTO blacklist(
                  phone_hmac,phone_enc,phone_mask,key_version,source,remark,created_by
                ) VALUES(
                  :canonical,:phone_enc,:phone_mask,:key_version,:source,:remark,:actor
                )
                """
            ),
            new_rows,
        )
    canonical_hmacs = [canonical for canonical, _aliases in canonical_aliases]
    await connection.execute(
        text(
            "DELETE FROM blacklist_hmac_alias "
            "WHERE blacklist_digest=ANY(CAST(:hmacs AS char(64)[]))"
        ),
        {"hmacs": canonical_hmacs},
    )
    alias_rows = [
        {
            "canonical": canonical,
            "version": version,
            "digest": digest,
        }
        for canonical, aliases in canonical_aliases
        for version, digest in aliases
    ]
    await connection.execute(
        text(
            """
            INSERT INTO blacklist_hmac_alias(
              blacklist_digest,hmac_key_version,hmac_digest
            ) VALUES(:canonical,:version,:digest)
            ON CONFLICT(hmac_key_version,hmac_digest) DO UPDATE SET
              blacklist_digest=excluded.blacklist_digest
            """
        ),
        alias_rows,
    )
    await bind_connection_audit_subject(
        connection,
        subject_kind="human",
        actor_name=principal.login_name,
        account_id=principal.account_id,
        identity_id=principal.identity_id,
    )
    await insert_audit(
        connection,
        AuditEvent(
            principal=principal,
            role=principal.role,
            ip=ip,
            action=audit_action,
            object_type="blacklist",
            object_id=audit_object_id,
            after={"count": len(entries), "source": source},
        ),
    )
    return BlacklistUpsertResult(added=len(entries) - updated, updated=updated)


class SqlBlacklistRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_page(
        self,
        *,
        source: str | None,
        keyword: str | None,
        page: int,
        size: int,
    ) -> BlacklistPage:
        where = ""
        params: dict[str, Any] = {"limit": size, "offset": (page - 1) * size}
        conditions: list[str] = []
        if source is not None:
            conditions.append("source=:source")
            params["source"] = source
        if keyword is not None:
            conditions.append("(phone_mask ILIKE :keyword OR remark ILIKE :keyword)")
            params["keyword"] = f"%{_escape_like(keyword)}%"
        if conditions:
            where = " WHERE " + " AND ".join(conditions)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                total = await connection.scalar(
                    text(f"SELECT count(*) FROM blacklist{where}"),  # noqa: S608
                    params,
                )
                result = await connection.execute(
                    text(
                        f"""
                        SELECT phone_hmac,phone_enc,phone_mask,key_version,source,remark,created_at
                        FROM blacklist{where} ORDER BY created_at DESC
                        LIMIT :limit OFFSET :offset
                        """  # noqa: S608
                    ),
                    params,
                )
                return BlacklistPage(
                    total=int(total or 0),
                    items=[BlacklistEntry(**dict(row)) for row in result.mappings()],
                )
        finally:
            await engine.dispose()

    async def all_hmacs(self) -> set[str]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT hmac_digest FROM blacklist_hmac_alias")
                )
                return {str(value).strip() for value in result.scalars()}
        finally:
            await engine.dispose()

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        principal: SecurityPrincipal,
        ip: str,
        source: str,
    ) -> BlacklistUpsertResult:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                return await upsert_blacklist_entries(
                    connection,
                    entries,
                    principal=principal,
                    ip=ip,
                    source=source,
                )
        finally:
            await engine.dispose()

    async def delete(
        self,
        phone_hmac: str,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        DELETE FROM blacklist
                        WHERE phone_hmac=COALESCE(
                          (
                            SELECT blacklist_digest FROM blacklist_hmac_alias
                            WHERE hmac_digest=:phone_hmac
                          ),
                          CAST(:phone_hmac AS char(64))
                        )
                        RETURNING 1
                        """
                    ),
                    {"phone_hmac": phone_hmac},
                )
                removed = result.scalar_one_or_none() is not None
                if removed:
                    await bind_connection_audit_subject(
                        connection,
                        subject_kind="human",
                        actor_name=principal.login_name,
                        account_id=principal.account_id,
                        identity_id=principal.identity_id,
                    )
                    await insert_audit(
                        connection,
                        AuditEvent(
                            principal=principal,
                            role=principal.role,
                            ip=ip,
                            action="blacklist_delete",
                            object_type="blacklist",
                            object_id="batch",
                            after={"count": 1},
                        ),
                    )
                return removed
        finally:
            await engine.dispose()
