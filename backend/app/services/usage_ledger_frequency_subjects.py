"""号码频控主体：HMAC alias 绑定与主体确保，批量路径避免逐号确保。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.usage_ledger_common import (
    FrequencyDecisionItem,
    ResolvedFrequencySubject,
    UsageReservationConflict,
)
from app.services.usage_ledger_frequency_merge import (
    _lock_frequency_merge_keys,
    _merge_frequency_projections,
)
from app.services.usage_ledger_projection import (
    ProjectionRow,
)


async def _load_frequency_alias_map(
    connection: AsyncConnection,
    digests: Sequence[str],
) -> dict[str, UUID]:
    unique = list(dict.fromkeys(digests))
    if not unique:
        return {}
    result = await connection.execute(
        text(
            """
            SELECT phone_hmac,subject_id FROM usage_frequency_alias
            WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
            """
        ),
        {"digests": unique},
    )
    return {str(row["phone_hmac"]): UUID(str(row["subject_id"])) for row in result.mappings()}


async def _load_frequency_subject_rows(
    connection: AsyncConnection,
    subject_ids: Sequence[UUID],
) -> dict[UUID, Any]:
    unique = list(dict.fromkeys(subject_ids))
    if not unique:
        return {}
    result = await connection.execute(
        text(
            """
            SELECT id,projection_hmac FROM usage_frequency_subject
            WHERE id=ANY(CAST(:subject_ids AS uuid[]))
            ORDER BY projection_hmac,id
            """
        ),
        {"subject_ids": unique},
    )
    return {UUID(str(row["id"])): row for row in result.mappings()}


async def _bind_frequency_aliases_many(
    connection: AsyncConnection,
    bindings: Sequence[tuple[UUID, Mapping[int, str]]],
) -> None:
    if not bindings:
        return
    alias_payload = json.dumps(
        [
            {
                "subject_id": str(subject_id),
                "key_version": version,
                "phone_hmac": digest,
            }
            for subject_id, hmac_aliases in bindings
            for version, digest in sorted(hmac_aliases.items())
        ],
        separators=(",", ":"),
    )
    await connection.execute(
        text(
            """
            INSERT INTO usage_frequency_alias(
              subject_id,key_version,phone_hmac
            )
            SELECT
              alias.subject_id,alias.key_version,alias.phone_hmac
            FROM jsonb_to_recordset(CAST(:aliases AS jsonb)) AS alias(
              subject_id uuid,key_version smallint,phone_hmac char(64)
            )
            ON CONFLICT(phone_hmac) DO NOTHING
            """
        ),
        {"aliases": alias_payload},
    )


def _item_subject_ids(
    item: FrequencyDecisionItem,
    alias_map: Mapping[str, UUID],
) -> list[UUID]:
    found: list[UUID] = []
    seen: set[UUID] = set()
    for digest in item.hmac_aliases.values():
        subject_id = alias_map.get(digest)
        if subject_id is None or subject_id in seen:
            continue
        seen.add(subject_id)
        found.append(subject_id)
    return found


async def _list_frequency_subjects(
    connection: AsyncConnection,
    digests: Sequence[str],
) -> tuple[set[UUID], list[Any]]:
    alias_result = await connection.execute(
        text(
            """
            SELECT DISTINCT subject_id FROM usage_frequency_alias
            WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
            """
        ),
        {"digests": list(digests)},
    )
    subject_ids = {UUID(str(value)) for value in alias_result.scalars()}
    if not subject_ids:
        return set(), []
    subject_result = await connection.execute(
        text(
            """
            SELECT id,projection_hmac FROM usage_frequency_subject
            WHERE id=ANY(CAST(:subject_ids AS uuid[]))
            ORDER BY projection_hmac,id
            """
        ),
        {"subject_ids": list(subject_ids)},
    )
    return subject_ids, list(subject_result.mappings())


async def _bind_frequency_aliases(
    connection: AsyncConnection,
    *,
    subject_id: UUID,
    hmac_aliases: Mapping[int, str],
) -> None:
    alias_payload = json.dumps(
        [
            {"key_version": version, "phone_hmac": digest}
            for version, digest in sorted(hmac_aliases.items())
        ],
        separators=(",", ":"),
    )
    await connection.execute(
        text(
            """
            INSERT INTO usage_frequency_alias(
              subject_id,key_version,phone_hmac
            )
            SELECT
              :subject_id,alias.key_version,alias.phone_hmac
            FROM jsonb_to_recordset(CAST(:aliases AS jsonb)) AS alias(
              key_version smallint,phone_hmac char(64)
            )
            ON CONFLICT(phone_hmac) DO NOTHING
            """
        ),
        {"subject_id": subject_id, "aliases": alias_payload},
    )
    alias_check = await connection.execute(
        text(
            """
            SELECT count(DISTINCT subject_id) FROM usage_frequency_alias
            WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
            """
        ),
        {"digests": list(hmac_aliases.values())},
    )
    if int(alias_check.scalar_one()) != 1:
        raise UsageReservationConflict("frequency alias write conflict")


async def _ensure_frequency_subjects_many(
    connection: AsyncConnection,
    items: Sequence[FrequencyDecisionItem],
) -> tuple[list[ResolvedFrequencySubject], tuple[ProjectionRow, ...]]:
    """整块定位/创建频控主体；SQL 次数随块数增长，不随号码线性放大。"""

    if not items:
        return [], ()
    unique_items: list[FrequencyDecisionItem] = []
    original_to_unique: list[int] = []
    seen_hmac: dict[str, int] = {}
    for item in items:
        existing_index = seen_hmac.get(item.phone_hmac)
        if existing_index is not None:
            original_to_unique.append(existing_index)
            continue
        seen_hmac[item.phone_hmac] = len(unique_items)
        original_to_unique.append(len(unique_items))
        unique_items.append(item)
    all_digests = [digest for item in unique_items for digest in item.hmac_aliases.values()]
    alias_map = await _load_frequency_alias_map(connection, all_digests)
    found_subject_ids = [
        subject_id for item in unique_items for subject_id in _item_subject_ids(item, alias_map)
    ]
    subject_rows = await _load_frequency_subject_rows(connection, found_subject_ids)

    resolved: list[ResolvedFrequencySubject | None] = [None] * len(unique_items)
    merged_rows: list[ProjectionRow] = []
    create_rows: list[tuple[int, FrequencyDecisionItem, UUID, str]] = []
    bind_rows: list[tuple[int, FrequencyDecisionItem, UUID, str]] = []
    for index, item in enumerate(unique_items):
        found = _item_subject_ids(item, alias_map)
        if not found:
            subject_id = uuid4()
            projection_hmac = item.hmac_aliases[min(item.hmac_aliases)]
            create_rows.append((index, item, subject_id, projection_hmac))
            continue
        if len(found) > 1:
            subject_id, projection_hmac, merged = await _ensure_frequency_subject(connection, item)
            merged_rows.extend(merged)
            resolved[index] = ResolvedFrequencySubject(
                item.phone_hmac,
                item.hmac_aliases,
                subject_id,
                projection_hmac,
            )
            continue
        subject_id = found[0]
        row = subject_rows.get(subject_id)
        if row is None:
            raise UsageReservationConflict("frequency subject unavailable")
        bind_rows.append((index, item, subject_id, str(row["projection_hmac"])))

    if create_rows:
        await connection.execute(
            text(
                """
                INSERT INTO usage_frequency_subject(id,projection_hmac)
                SELECT item.id,item.projection_hmac
                FROM jsonb_to_recordset(CAST(:subjects AS jsonb)) AS item(
                  id uuid,projection_hmac char(64)
                )
                """
            ),
            {
                "subjects": json.dumps(
                    [
                        {
                            "id": str(subject_id),
                            "projection_hmac": projection_hmac,
                        }
                        for _index, _item, subject_id, projection_hmac in create_rows
                    ],
                    separators=(",", ":"),
                )
            },
        )
        bind_rows.extend(create_rows)

    await _bind_frequency_aliases_many(
        connection,
        [(subject_id, item.hmac_aliases) for _index, item, subject_id, _hmac in bind_rows],
    )
    if bind_rows:
        verify_digests = [
            digest
            for _index, item, _sid, _hmac in bind_rows
            for digest in item.hmac_aliases.values()
        ]
        verify_map = await _load_frequency_alias_map(connection, verify_digests)
        for index, item, intended_id, projection_hmac in bind_rows:
            found = _item_subject_ids(item, verify_map)
            if found != [intended_id]:
                raise UsageReservationConflict("frequency alias write conflict")
            resolved[index] = ResolvedFrequencySubject(
                item.phone_hmac,
                item.hmac_aliases,
                intended_id,
                projection_hmac,
            )

    if any(item is None for item in resolved):
        raise UsageReservationConflict("frequency subject unavailable")
    unique_resolved = [item for item in resolved if item is not None]
    return [unique_resolved[index] for index in original_to_unique], tuple(merged_rows)


async def _ensure_frequency_subject(
    connection: AsyncConnection,
    item: FrequencyDecisionItem,
) -> tuple[UUID, str, tuple[ProjectionRow, ...]]:
    """定位或创建频控主体，必要时原子归并投影，返回 (subject, hmac, 变更投影)。"""

    hmac_aliases = dict(item.hmac_aliases)
    digests = list(hmac_aliases.values())
    subject_ids, subject_rows = await _list_frequency_subjects(connection, digests)
    merged: tuple[ProjectionRow, ...] = ()
    if subject_ids:
        if len(subject_rows) != len(subject_ids):
            raise UsageReservationConflict("frequency subject unavailable")
        preferred_subject = await connection.scalar(
            text(
                """
                SELECT subject_id FROM usage_frequency_alias
                WHERE phone_hmac=:preferred_digest
                """
            ),
            {"preferred_digest": hmac_aliases[min(hmac_aliases)]},
        )
        if len(subject_rows) > 1:
            await _lock_frequency_merge_keys(
                connection,
                [str(row["projection_hmac"]) for row in subject_rows],
                [UUID(str(row["id"])) for row in subject_rows],
            )
            subject_ids, subject_rows = await _list_frequency_subjects(connection, digests)
            if len(subject_rows) != len(subject_ids):
                raise UsageReservationConflict("frequency subject unavailable")
            preferred_subject = await connection.scalar(
                text(
                    """
                    SELECT subject_id FROM usage_frequency_alias
                    WHERE phone_hmac=:preferred_digest
                    """
                ),
                {"preferred_digest": hmac_aliases[min(hmac_aliases)]},
            )
        preferred_subject_id = (
            UUID(str(preferred_subject)) if preferred_subject is not None else None
        )
        canonical = next(
            (row for row in subject_rows if UUID(str(row["id"])) == preferred_subject_id),
            subject_rows[0],
        )
        subject_id = UUID(str(canonical["id"]))
        projection_hmac = str(canonical["projection_hmac"])
        if len(subject_rows) > 1:
            merged = await _merge_frequency_projections(
                connection,
                canonical_hmac=projection_hmac,
                subject_rows=subject_rows,
            )
            for source in subject_rows:
                source_id = UUID(str(source["id"]))
                if source_id == subject_id:
                    continue
                await connection.execute(
                    text(
                        """
                        UPDATE usage_frequency_alias SET subject_id=:target_id
                        WHERE subject_id=:source_id
                        """
                    ),
                    {"target_id": subject_id, "source_id": source_id},
                )
                await connection.execute(
                    text(
                        """
                        UPDATE usage_frequency_entry SET subject_id=:target_id
                        WHERE subject_id=:source_id
                        """
                    ),
                    {"target_id": subject_id, "source_id": source_id},
                )
                await connection.execute(
                    text("DELETE FROM usage_frequency_subject WHERE id=:source_id"),
                    {"source_id": source_id},
                )
    else:
        subject_id = uuid4()
        projection_hmac = hmac_aliases[min(hmac_aliases)]
        await connection.execute(
            text(
                """
                INSERT INTO usage_frequency_subject(id,projection_hmac)
                VALUES(:id,:projection_hmac)
                """
            ),
            {"id": subject_id, "projection_hmac": projection_hmac},
        )
    await _bind_frequency_aliases(
        connection,
        subject_id=subject_id,
        hmac_aliases=hmac_aliases,
    )
    return subject_id, projection_hmac, merged
