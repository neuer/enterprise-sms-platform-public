"""真实联调测试号码 PostgreSQL 事实源与无 PII 审计。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.vendor_test_lifecycle import lock_vendor_test_lifecycle
from app.services.vendor_test_recipient import (
    DuplicateVendorTestRecipient,
    InvalidVendorTestRecipient,
    RecipientBusy,
    RecipientHmacIndexStale,
    RecipientNotFound,
    VendorTestRecipientCreate,
    VendorTestRecipientForSend,
    VendorTestRecipientRecord,
    VendorTestRecipientSummary,
)
from app.settings import Settings, get_settings

_SUMMARY_COLUMNS = "id,label,phone_mask,status,created_at,disabled_at"
VENDOR_TEST_RECIPIENT_LOCK = "vendor-test-recipient"


async def lock_vendor_test_recipient_maintenance(connection: Any) -> None:
    """串行化测试号码维护与 worker 的最后一次 active 校验。"""

    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
        {"lock_name": VENDOR_TEST_RECIPIENT_LOCK},
    )


def _one_or_none(result: Any) -> Any:
    return result.mappings().one_or_none()


def _summary(row: Any) -> VendorTestRecipientSummary:
    return VendorTestRecipientSummary(
        id=int(row["id"]),
        label=str(row["label"]),
        phone_mask=str(row["phone_mask"]),
        status=str(row["status"]),
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


class SqlVendorTestRecipientRepository:
    """加密写入与列表投影分离，只有 resolve_for_send 选择密文。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    @staticmethod
    async def _lock(connection: Any) -> None:
        await lock_vendor_test_recipient_maintenance(connection)

    @staticmethod
    async def _has_active_uat(connection: Any) -> bool:
        result = await connection.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM vendor_test_operation
                  WHERE operation_type='uat_send' AND status IN ('requested','running')
                ) OR EXISTS (
                  SELECT 1 FROM sms_batch
                  WHERE is_test=true AND status IN ('queued','sending')
                )
                """
            )
        )
        return bool(result.scalar_one())

    @staticmethod
    async def _has_active_reset(connection: Any) -> bool:
        result = await connection.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM vendor_test_operation
                  WHERE operation_type='reset_configuration'
                    AND status IN ('requested','running')
                )
                """
            )
        )
        return bool(result.scalar_one())

    async def create(
        self,
        candidate: VendorTestRecipientCreate,
        candidates: dict[int, str],
        *,
        actor: str,
    ) -> VendorTestRecipientRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_vendor_test_lifecycle(connection)
                await self._lock(connection)
                if await self._has_active_reset(connection):
                    raise RecipientBusy("配置重置尚未完成，暂不能登记测试号码")
                conditions: list[str] = []
                duplicate_params: dict[str, object] = {}
                for index, (version, digest) in enumerate(sorted(candidates.items())):
                    conditions.append(
                        f"(hmac_key_version=:candidate_version_{index} "
                        f"AND hmac_digest=:candidate_hmac_{index})"
                    )
                    duplicate_params[f"candidate_version_{index}"] = version
                    duplicate_params[f"candidate_hmac_{index}"] = digest
                duplicate = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 "
                        "FROM vendor_test_recipient_hmac_alias WHERE "
                        + " OR ".join(conditions)
                        + ")"
                    ),
                    duplicate_params,
                )
                if bool(duplicate.scalar_one()):
                    raise DuplicateVendorTestRecipient("测试号码已登记")
                inserted = await connection.execute(
                    text(
                        """
                        INSERT INTO vendor_test_recipient(
                          label,phone_enc,phone_hmac,phone_mask,key_version,created_by
                        ) VALUES(
                          :label,:phone_enc,:phone_hmac,:phone_mask,:key_version,:actor
                        ) RETURNING id,status,created_at
                        """
                    ),
                    {
                        "label": candidate.label,
                        "phone_enc": candidate.phone_enc,
                        "phone_hmac": candidate.phone_hmac,
                        "phone_mask": candidate.phone_mask,
                        "key_version": candidate.key_version,
                        "actor": actor,
                    },
                )
                row = _one_or_none(inserted)
                if row is None:
                    raise RuntimeError("测试号码写入失败")
                record = VendorTestRecipientRecord(
                    id=int(row["id"]),
                    label=candidate.label,
                    phone_enc=candidate.phone_enc,
                    phone_hmac=candidate.phone_hmac,
                    phone_mask=candidate.phone_mask,
                    key_version=candidate.key_version,
                    status=str(row["status"]),
                    created_by=actor,
                    created_at=row["created_at"],
                )
                await self._insert_aliases(connection, record.id, candidates)
                await self._audit(connection, actor, "vendor_test_recipient_add", record.id)
                return record
        finally:
            await engine.dispose()

    async def list_summaries(
        self,
        *,
        include_disabled: bool = True,
    ) -> tuple[VendorTestRecipientSummary, ...]:
        engine = self._engine()
        try:
            where = "" if include_disabled else "WHERE status='active'"
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"SELECT {_SUMMARY_COLUMNS} FROM vendor_test_recipient "
                        f"{where} ORDER BY created_at DESC,id DESC"
                    )
                )
                return tuple(_summary(row) for row in result.mappings())
        finally:
            await engine.dispose()

    async def disable(
        self,
        recipient_id: int,
        *,
        actor: str,
    ) -> VendorTestRecipientSummary:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await self._lock(connection)
                locked = await connection.execute(
                    text("SELECT id,status FROM vendor_test_recipient WHERE id=:id FOR UPDATE"),
                    {"id": recipient_id},
                )
                if _one_or_none(locked) is None:
                    raise RecipientNotFound("测试号码不存在")
                if await self._has_active_uat(connection):
                    raise RecipientBusy("存在活动真实 UAT，暂不能停用测试号码")
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE vendor_test_recipient
                        SET status='disabled',disabled_by=:actor,disabled_at=now()
                        WHERE id=:id
                        RETURNING {_SUMMARY_COLUMNS}
                        """
                    ),
                    {"id": recipient_id, "actor": actor},
                )
                row = _one_or_none(updated)
                if row is None:
                    raise RecipientNotFound("测试号码不存在")
                await self._audit(
                    connection,
                    actor,
                    "vendor_test_recipient_disable",
                    recipient_id,
                )
                return _summary(row)
        finally:
            await engine.dispose()

    async def delete(self, recipient_id: int, *, actor: str) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await self._lock(connection)
                if await self._has_active_uat(connection):
                    raise RecipientBusy("存在活动真实 UAT，暂不能删除测试号码")
                removed = await connection.execute(
                    text("DELETE FROM vendor_test_recipient WHERE id=:id RETURNING id"),
                    {"id": recipient_id},
                )
                deleted = removed.scalar_one_or_none() is not None
                if deleted:
                    await self._audit(
                        connection,
                        actor,
                        "vendor_test_recipient_delete",
                        recipient_id,
                    )
                return deleted
        finally:
            await engine.dispose()

    async def purge_all(self, *, actor: str) -> int:
        """在同一事务中删除全部加密测试号码并记录一次 count-only 审计。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_vendor_test_lifecycle(connection)
                await self._lock(connection)
                if await self._has_active_uat(connection):
                    raise RecipientBusy("存在活动真实 UAT，暂不能清空测试号码")
                removed = await connection.execute(
                    text("DELETE FROM vendor_test_recipient RETURNING id")
                )
                count = sum(1 for _ in removed.mappings())
                await self._audit(
                    connection,
                    actor,
                    "vendor_test_recipient_purge_all",
                    "all",
                    after={"count": count},
                )
                return count
        finally:
            await engine.dispose()

    async def resolve_for_send(self, recipient_id: int) -> VendorTestRecipientForSend:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,phone_enc,phone_hmac,phone_mask,key_version
                        FROM vendor_test_recipient
                        WHERE id=:id AND status='active'
                        """
                    ),
                    {"id": recipient_id},
                )
                rows = list(result.mappings())
                if not rows:
                    raise RecipientNotFound("测试号码不存在或已停用")
                if len(rows) != 1:
                    raise RecipientHmacIndexStale("测试号码索引待刷新")
                row = rows[0]
                alias_result = await connection.execute(
                    text(
                        """
                        SELECT hmac_key_version AS key_version,
                               hmac_digest AS phone_hmac
                        FROM vendor_test_recipient_hmac_alias
                        WHERE recipient_id=:id
                        ORDER BY key_version
                        """
                    ),
                    {"id": recipient_id},
                )
                aliases = tuple(
                    (int(alias["key_version"]), str(alias["phone_hmac"]))
                    for alias in alias_result.mappings()
                )
                return VendorTestRecipientForSend(
                    id=int(row["id"]),
                    phone_enc=bytes(row["phone_enc"]),
                    phone_hmac=str(row["phone_hmac"]),
                    phone_mask=str(row["phone_mask"]),
                    key_version=int(row["key_version"]),
                    hmac_candidates=aliases,
                )
        finally:
            await engine.dispose()

    async def resolve_by_hmac_candidates(
        self,
        candidates: dict[int, str],
    ) -> VendorTestRecipientForSend:
        """仅以版本化 HMAC 定位 active 号码，明文不得进入 SQL 或参数。"""

        if not candidates:
            raise RecipientNotFound("测试号码不存在或已停用")
        conditions, params = self._candidate_conditions(candidates)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT DISTINCT r.id,r.phone_enc,r.phone_hmac,
                               r.phone_mask,r.key_version
                        FROM vendor_test_recipient r
                        JOIN vendor_test_recipient_hmac_alias a
                          ON a.recipient_id=r.id
                        WHERE r.status='active' AND (
                        """
                        + " OR ".join(
                            condition.replace("hmac_key_version", "a.hmac_key_version").replace(
                                "hmac_digest", "a.hmac_digest"
                            )
                            for condition in conditions
                        )
                        + ")"
                    ),
                    params,
                )
                rows = list(result.mappings())
                if not rows:
                    raise RecipientNotFound("测试号码不存在或已停用")
                if len(rows) != 1:
                    raise RecipientHmacIndexStale("测试号码索引待刷新")
                row = rows[0]
                alias_result = await connection.execute(
                    text(
                        """
                        SELECT hmac_key_version AS key_version,
                               hmac_digest AS phone_hmac
                        FROM vendor_test_recipient_hmac_alias
                        WHERE recipient_id=:id
                        ORDER BY key_version
                        """
                    ),
                    {"id": int(row["id"])},
                )
                aliases = tuple(
                    (int(alias["key_version"]), str(alias["phone_hmac"]))
                    for alias in alias_result.mappings()
                )
                return VendorTestRecipientForSend(
                    id=int(row["id"]),
                    phone_enc=bytes(row["phone_enc"]),
                    phone_hmac=str(row["phone_hmac"]),
                    phone_mask=str(row["phone_mask"]),
                    key_version=int(row["key_version"]),
                    hmac_candidates=aliases,
                )
        finally:
            await engine.dispose()

    async def refresh_hmac_candidates(
        self,
        recipient_id: int,
        candidates: dict[int, str],
        *,
        actor: str,
    ) -> VendorTestRecipientSummary:
        """重录号码须命中既有别名，随后在同一事务替换全部版本索引。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await self._lock(connection)
                locked = await connection.execute(
                    text(
                        f"SELECT {_SUMMARY_COLUMNS} FROM vendor_test_recipient "
                        "WHERE id=:id AND status='active' FOR UPDATE"
                    ),
                    {"id": recipient_id},
                )
                row = _one_or_none(locked)
                if row is None:
                    raise RecipientNotFound("测试号码不存在或已停用")
                conditions, params = self._candidate_conditions(candidates)
                params["recipient_id"] = recipient_id
                matched = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 "
                        "FROM vendor_test_recipient_hmac_alias "
                        "WHERE recipient_id=:recipient_id AND (" + " OR ".join(conditions) + "))"
                    ),
                    params,
                )
                if not bool(matched.scalar_one()):
                    raise InvalidVendorTestRecipient("输入号码与登记记录不匹配")
                duplicate = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 "
                        "FROM vendor_test_recipient_hmac_alias "
                        "WHERE recipient_id<>:recipient_id AND (" + " OR ".join(conditions) + "))"
                    ),
                    params,
                )
                if bool(duplicate.scalar_one()):
                    raise DuplicateVendorTestRecipient("测试号码已登记")
                await connection.execute(
                    text(
                        "DELETE FROM vendor_test_recipient_hmac_alias "
                        "WHERE recipient_id=:recipient_id"
                    ),
                    {"recipient_id": recipient_id},
                )
                await self._insert_aliases(connection, recipient_id, candidates)
                await self._audit(
                    connection,
                    actor,
                    "vendor_test_recipient_refresh_index",
                    recipient_id,
                    after={"count": 1, "versions": len(candidates)},
                )
                return _summary(row)
        finally:
            await engine.dispose()

    @staticmethod
    def _candidate_conditions(
        candidates: dict[int, str],
    ) -> tuple[list[str], dict[str, object]]:
        conditions: list[str] = []
        params: dict[str, object] = {}
        for index, (version, digest) in enumerate(sorted(candidates.items())):
            conditions.append(
                f"(hmac_key_version=:candidate_version_{index} "
                f"AND hmac_digest=:candidate_hmac_{index})"
            )
            params[f"candidate_version_{index}"] = version
            params[f"candidate_hmac_{index}"] = digest
        return conditions, params

    @staticmethod
    async def _insert_aliases(
        connection: Any,
        recipient_id: int,
        candidates: dict[int, str],
    ) -> None:
        values: list[str] = []
        params: dict[str, object] = {"recipient_id": recipient_id}
        for index, (version, digest) in enumerate(sorted(candidates.items())):
            values.append(f"(:recipient_id,:alias_version_{index},:alias_hmac_{index})")
            params[f"alias_version_{index}"] = version
            params[f"alias_hmac_{index}"] = digest
        await connection.execute(
            text(
                "INSERT INTO vendor_test_recipient_hmac_alias("
                "recipient_id,hmac_key_version,hmac_digest) VALUES " + ",".join(values)
            ),
            params,
        )

    @staticmethod
    async def _audit(
        connection: Any,
        actor: str,
        action: str,
        recipient_id: int | str,
        *,
        after: dict[str, int] | None = None,
    ) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                VALUES(:actor,:action,'vendor_test_recipient',:object_id,CAST(:after AS jsonb))
                """
            ),
            {
                "actor": actor,
                "action": action,
                "object_id": str(recipient_id),
                "after": json.dumps(after or {"count": 1}),
            },
        )
