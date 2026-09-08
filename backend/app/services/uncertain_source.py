"""人工 unknown 重发的进程内源资格证明；不进入 API、数据库或队列。"""

from __future__ import annotations

import hmac
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.crypto import CryptoService, ProtectedPhone


@dataclass(frozen=True, slots=True, repr=False)
class SourceMessage:
    id: int
    created_at: datetime
    batch_id: int
    chunk_id: int
    status: str
    report_status: int | None
    report_time: datetime | None
    report_event_key: str | None
    phone_hmac: str
    key_version: int
    recipient_aliases: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True, repr=False)
class UncertainSourceProof:
    resolution_id: int
    generation: int
    messages: tuple[SourceMessage, ...]
    key_version: int
    signature: str = field(repr=False)

    def canonical(self) -> bytes:
        """完整绑定源身份、报告证据和各保留版本的接收号码映射。"""
        return (
            b"uncertain-source:v1\0"
            + json.dumps(
                [self.resolution_id, self.generation, [asdict(row) for row in self.messages]],
                sort_keys=True,
                separators=(",", ":"),
                default=lambda value: value.isoformat(),
            ).encode()
        )


def prepare_source_proof(
    crypto: CryptoService,
    resolution_id: int,
    generation: int,
    rows: Sequence[Any],
    mobiles: Sequence[str],
) -> UncertainSourceProof:
    """仅由服务端读取并受控解密源消息后签发，沿用现有文件型 HMAC 密钥。"""
    from dataclasses import replace

    messages = tuple(
        SourceMessage(
            id=int(row["id"]),
            created_at=row["created_at"],
            batch_id=int(row["batch_id"]),
            chunk_id=int(row["chunk_id"]),
            status=str(row["status"]),
            report_status=row["report_status"],
            report_time=row["report_time"],
            report_event_key=row["report_event_key"],
            phone_hmac=str(row["phone_hmac"]).strip(),
            key_version=int(row["key_version"]),
            recipient_aliases=tuple(sorted(crypto.hmac_candidates(mobile).items())),
        )
        for row, mobile in zip(rows, mobiles, strict=True)
    )
    proof = UncertainSourceProof(resolution_id, generation, messages, crypto.active_version, "")
    return replace(
        proof,
        signature=crypto.idempotency_fingerprint(
            proof.canonical(),
            key_version=proof.key_version,
        ),
    )


async def verify_source_proof(
    connection: AsyncConnection,
    crypto: CryptoService,
    proof: UncertainSourceProof | None,
    *,
    resolution_id: int,
    generation: int,
    batch_id: int,
    chunk_id: int,
    accepted: Sequence[ProtectedPhone],
) -> None:
    """在 chunk→batch→resolution 锁之后锁消息，新语句复核后方可创建 child。"""
    from app.services.uncertain_resolution import SourceMessageStateChanged

    conflict = SourceMessageStateChanged("source_message_state_changed")
    if not isinstance(proof, UncertainSourceProof) or not proof.messages:
        raise conflict
    try:
        valid_signature = hmac.compare_digest(
            proof.signature,
            crypto.idempotency_fingerprint(
                proof.canonical(),
                key_version=proof.key_version,
            ),
        )
    except (ValueError, KeyError, TypeError):
        raise conflict from None
    if not valid_signature or (proof.resolution_id, proof.generation) != (
        resolution_id,
        generation,
    ):
        raise conflict
    identities = {(row.id, row.created_at) for row in proof.messages}
    if len(identities) != len(proof.messages) or any(
        (row.batch_id, row.chunk_id) != (batch_id, chunk_id)
        or row.status != "unknown"
        or row.report_status == 1
        for row in proof.messages
    ):
        raise conflict
    groups = {row.recipient_aliases for row in proof.messages}
    aliases = {alias for group in groups for alias in group}
    # 正常去重、黑名单与频控可移除号码；任何新增或换号都不可越过源证明。
    if not accepted or any((row.key_version, row.phone_hmac) not in aliases for row in accepted):
        raise conflict
    matched = [
        {group for group in groups if (row.key_version, row.phone_hmac) in group}
        for row in accepted
    ]
    if any(len(group) != 1 for group in matched) or len(
        {next(iter(group)) for group in matched}
    ) != len(accepted):
        raise conflict
    parameters = {"ids": [row.id for row in proof.messages], "batch": batch_id, "chunk": chunk_id}
    where = "WHERE id=ANY(:ids) AND batch_id=:batch AND chunk_id=:chunk"
    await connection.execute(
        text("SELECT id FROM sms_message " + where + " ORDER BY id,created_at FOR UPDATE"),
        parameters,
    )
    rows = (
        (
            await connection.execute(
                text(
                    "SELECT id,created_at,batch_id,chunk_id,status,report_status,report_time,"
                    "report_event_key,trim(phone_hmac) phone_hmac,key_version FROM sms_message "
                    + where
                ),
                parameters,
            )
        )
        .mappings()
        .all()
    )
    current = {(row["id"], row["created_at"]): row for row in rows}
    if len(rows) != len(identities) or set(current) != identities:
        raise conflict
    for original in proof.messages:
        row = current[(original.id, original.created_at)]
        if any(
            row[key] != value
            for key, value in asdict(original).items()
            if key != "recipient_aliases"
        ):
            raise conflict
