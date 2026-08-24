"""只读验证恢复库与当前恢复密钥包确实能够解密历史数据。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.audit_context import decode_audit_context_key
from app.services.crypto import CryptoService, EncryptionContext
from app.settings import Settings, get_settings

AUDIT_KEY_NAMES = {
    "principal": "audit_context_key",
    "system:api": "audit_system_api_context_key",
    "system:realtime": "audit_system_realtime_context_key",
    "system:bulk": "audit_system_bulk_context_key",
}
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
CipherKind = Literal["phone", "packed_text", "raw_payload"]


class RecoveryCryptoProbeError(RuntimeError):
    """探针失败；消息保持固定，不包含密钥、密文或手机号。"""


def _bytes(value: object, label: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise RecoveryCryptoProbeError(f"{label} is invalid")
    return bytes(value)


@dataclass(frozen=True, slots=True)
class ProbeCounts:
    audit_context_keys: int
    encrypted_columns: int
    encrypted_rows: int
    ciphertext_samples_verified: int
    key_version_columns: int
    referenced_key_versions: int
    sms_message_rows: int


@dataclass(frozen=True, slots=True)
class ProbeCoverage:
    rows: int
    key_versions_verified: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    counts: ProbeCounts
    coverage: Mapping[str, ProbeCoverage]


@dataclass(frozen=True, slots=True)
class CipherSpec:
    """恢复探针的显式持久化密文闭集。"""

    table: str
    column: str
    kind: CipherKind
    domain: str
    context_table: str
    source_sql: str
    payload_expression: str
    version_expression: str
    verification_expression: str
    order_expression: str
    expected_digest_expression: str = "NULL::text"

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}"


def _packed_version(expression: str) -> str:
    return f"((get_byte({expression},0) << 8) + get_byte({expression},1))"


PHONE_SPECS = (
    ("vendor_test_recipient", "vendor_test_recipient", "id"),
    ("sms_message", "sms_message", "created_at,id"),
    # sms_reply 是 reply_event 的兼容投影，复制同一份绑定 reply_event 的密文。
    ("sms_reply", "reply_event", "created_at,id"),
    # report_event 与 unmatched_report 都由 report ingest 的 unmatched_report
    # 上下文产生；匹配投影不会重加密。
    ("report_event", "unmatched_report", "event_key"),
    ("reply_event", "reply_event", "event_key"),
    ("unmatched_report", "unmatched_report", "id"),
    ("import_phone", "import_phone", "id"),
    ("blacklist", "blacklist", "phone_hmac"),
)


def _phone_spec(
    table: str,
    context_table: str,
    order_expression: str,
) -> CipherSpec:
    return CipherSpec(
        table=table,
        column="phone_enc",
        kind="phone",
        domain="phone",
        context_table=context_table,
        source_sql=table,
        payload_expression="phone_enc",
        version_expression="key_version",
        verification_expression="trim(phone_hmac)",
        order_expression=order_expression,
    )


CIPHER_SPECS = tuple(_phone_spec(*item) for item in PHONE_SPECS) + (
    CipherSpec(
        "app",
        "callback_secret_enc",
        "packed_text",
        "callback-secret",
        "app",
        "app",
        "callback_secret_enc",
        _packed_version("callback_secret_enc"),
        "name",
        "id",
    ),
    CipherSpec(
        "sms_batch",
        "display_content_enc",
        "packed_text",
        "sms-display-content",
        "sms_batch",
        "sms_batch",
        "display_content_enc",
        _packed_version("display_content_enc"),
        "trim(batch_no)",
        "id",
    ),
    CipherSpec(
        "sms_batch",
        "send_content_enc",
        "packed_text",
        "sms-content",
        "sms_batch",
        "sms_batch",
        "send_content_enc",
        _packed_version("send_content_enc"),
        "trim(batch_no)",
        "id",
    ),
    CipherSpec(
        "raw_vendor_log",
        "payload_enc",
        "raw_payload",
        "vendor-raw",
        "raw_vendor_log",
        "raw_vendor_log",
        "payload_enc",
        "key_version",
        "source || ':' || trim(payload_sha256)",
        "id",
        "trim(payload_sha256)",
    ),
    CipherSpec(
        "reply_event",
        "content_enc",
        "packed_text",
        "reply-content",
        "reply_event",
        "reply_event",
        "content_enc",
        _packed_version("content_enc"),
        "trim(event_key)",
        "event_key",
    ),
    CipherSpec(
        "sms_template",
        "name_enc",
        "packed_text",
        "sms-template-name",
        "sms_template",
        "sms_template",
        "name_enc",
        _packed_version("name_enc"),
        "id::text",
        "id",
    ),
    CipherSpec(
        "sms_template",
        "content_enc",
        "packed_text",
        "sms-template-content",
        "sms_template",
        "sms_template",
        "content_enc",
        _packed_version("content_enc"),
        "id::text",
        "id",
    ),
    CipherSpec(
        "sensitive_metadata_archive",
        "value_enc",
        "packed_text",
        "sensitive-metadata-archive",
        "sensitive_metadata_archive",
        "sensitive_metadata_archive",
        "value_enc",
        _packed_version("value_enc"),
        "source_table || ':' || source_row || ':' || source_column",
        "source_table,source_row,source_column",
    ),
    # callback_task 固化 app.callback_secret_enc；AAD 始终绑定不可变 app.name。
    CipherSpec(
        "callback_task",
        "callback_secret_enc",
        "packed_text",
        "callback-secret",
        "app",
        "callback_task t JOIN app a ON a.id=t.app_id",
        "t.callback_secret_enc",
        _packed_version("t.callback_secret_enc"),
        "a.name",
        "t.id",
    ),
)
EXPECTED_ENCRYPTED_COLUMNS = frozenset(spec.label for spec in CIPHER_SPECS)


def _verify_audit_keys(
    rows: Sequence[tuple[object, object]],
    expected: Mapping[str, bytes],
) -> int:
    """用 constant-time 比较四个恢复审计 key，拒绝缺失、额外或重复。"""

    actual: dict[str, bytes] = {}
    for raw_kind, raw_material in rows:
        if not isinstance(raw_kind, str) or raw_kind not in expected:
            raise RecoveryCryptoProbeError("audit context key binding failed")
        if raw_kind in actual:
            raise RecoveryCryptoProbeError("audit context key binding failed")
        actual[raw_kind] = _bytes(raw_material, "audit context key binding")
    if set(actual) != set(expected):
        raise RecoveryCryptoProbeError("audit context key binding failed")
    if not all(
        hmac.compare_digest(actual[kind], expected[kind]) for kind in sorted(expected)
    ):
        raise RecoveryCryptoProbeError("audit context key binding failed")
    return len(actual)


def _verify_version_inventory(
    inventories: Sequence[Sequence[object]],
    available_versions: Sequence[int],
) -> tuple[int, int]:
    """确认每个持久化 key_version 引用都存在于当前 AES/HMAC keyring。"""

    available = set(available_versions)
    if not available or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in available
    ):
        raise RecoveryCryptoProbeError("recovery key version inventory is invalid")
    referenced: set[int] = set()
    for values in inventories:
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RecoveryCryptoProbeError(
                    "restored key version inventory is invalid"
                )
            referenced.add(value)
    if not referenced.issubset(available):
        raise RecoveryCryptoProbeError("recovery key version is unavailable")
    return len(inventories), len(referenced)


def _verify_phone_samples(
    rows: Sequence[tuple[object, object, object]],
    crypto: CryptoService,
    *,
    table: str,
) -> int:
    """每个表实际使用的 key_version 解密一条并同时复核 HMAC。"""

    seen: set[int] = set()
    try:
        for raw_version, raw_payload, raw_hmac in rows:
            if (
                isinstance(raw_version, bool)
                or not isinstance(raw_version, int)
                or raw_version < 1
                or raw_version in seen
                or not isinstance(raw_hmac, str)
            ):
                raise RecoveryCryptoProbeError(
                    "historical ciphertext sample is invalid"
                )
            plaintext = crypto.decrypt_phone(
                _bytes(raw_payload, "historical ciphertext sample"),
                raw_version,
                raw_hmac.strip(),
                table=table,
                allow_legacy=True,
            )
            # 明文只在当前循环局部内存存在；永不进入输出、异常或日志。
            del plaintext
            seen.add(raw_version)
    except RecoveryCryptoProbeError:
        raise
    except Exception as error:
        raise RecoveryCryptoProbeError(
            "historical ciphertext validation failed"
        ) from error
    return len(seen)


def _verify_packed_text_samples(
    rows: Sequence[tuple[object, object, object]],
    crypto: CryptoService,
    *,
    domain: str,
    table: str,
    column: str,
) -> int:
    """每个打包密文字段/版本以真实 AAD 解密一条。"""

    seen: set[int] = set()
    try:
        for raw_version, raw_payload, raw_object_id in rows:
            if (
                isinstance(raw_version, bool)
                or not isinstance(raw_version, int)
                or raw_version < 1
                or raw_version in seen
                or not isinstance(raw_object_id, str)
                or not raw_object_id
            ):
                raise RecoveryCryptoProbeError(
                    "historical ciphertext sample is invalid"
                )
            payload = _bytes(raw_payload, "historical ciphertext sample")
            if len(payload) <= 2 or int.from_bytes(payload[:2], "big") != raw_version:
                raise RecoveryCryptoProbeError(
                    "historical ciphertext sample is invalid"
                )
            plaintext = crypto.decrypt_bound_packed_text(
                payload,
                EncryptionContext(
                    domain=domain,
                    table=table,
                    column=column,
                    object_id=raw_object_id.strip(),
                ),
                allow_legacy=True,
            )
            del plaintext
            seen.add(raw_version)
    except RecoveryCryptoProbeError:
        raise
    except Exception as error:
        raise RecoveryCryptoProbeError(
            "historical ciphertext validation failed"
        ) from error
    return len(seen)


def _verify_raw_payload_samples(
    rows: Sequence[tuple[object, object, object, object]],
    crypto: CryptoService,
) -> int:
    """每个 raw payload 版本解密一条并复核原文 SHA-256。"""

    seen: set[int] = set()
    try:
        for raw_version, raw_payload, raw_object_id, raw_digest in rows:
            if (
                isinstance(raw_version, bool)
                or not isinstance(raw_version, int)
                or raw_version < 1
                or raw_version in seen
                or not isinstance(raw_object_id, str)
                or not isinstance(raw_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", raw_digest) is None
            ):
                raise RecoveryCryptoProbeError(
                    "historical ciphertext sample is invalid"
                )
            plaintext = crypto.decrypt_bound_bytes(
                _bytes(raw_payload, "historical ciphertext sample"),
                raw_version,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_vendor_log",
                    column="payload_enc",
                    object_id=raw_object_id,
                ),
                allow_legacy=True,
            )
            digest = hashlib.sha256(plaintext).hexdigest()
            del plaintext
            if not hmac.compare_digest(digest, raw_digest):
                raise RecoveryCryptoProbeError(
                    "historical ciphertext validation failed"
                )
            seen.add(raw_version)
    except RecoveryCryptoProbeError:
        raise
    except Exception as error:
        raise RecoveryCryptoProbeError(
            "historical ciphertext validation failed"
        ) from error
    return len(seen)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise RecoveryCryptoProbeError("encrypted column inventory is invalid")
    return value


async def _rows(connection: AsyncConnection, sql: str) -> list[tuple[Any, ...]]:
    result = await connection.execute(text(sql))
    return [tuple(row) for row in result.fetchall()]


def _count(rows: Sequence[tuple[object, ...]], label: str) -> int:
    if len(rows) != 1 or len(rows[0]) != 1:
        raise RecoveryCryptoProbeError(f"{label} count is invalid")
    value = rows[0][0]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryCryptoProbeError(f"{label} count is invalid")
    return value


def _verify_encrypted_column_inventory(
    rows: Sequence[tuple[object, object]],
) -> int:
    actual: set[str] = set()
    for raw_table, raw_column in rows:
        table = _identifier(raw_table)
        column = _identifier(raw_column)
        label = f"{table}.{column}"
        if label in actual:
            raise RecoveryCryptoProbeError("encrypted column inventory is invalid")
        actual.add(label)
    if actual != EXPECTED_ENCRYPTED_COLUMNS:
        raise RecoveryCryptoProbeError("encrypted column inventory is invalid")
    return len(actual)


async def _version_inventory(
    connection: AsyncConnection,
    crypto: CryptoService,
) -> tuple[int, int]:
    column_rows = await _rows(
        connection,
        "SELECT c.relname,a.attname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p') "
        "AND a.attnum>0 AND NOT a.attisdropped "
        "AND a.attname ~ '(^|_)key_version$' "
        "AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid) "
        "ORDER BY c.relname,a.attname",
    )
    if not column_rows:
        raise RecoveryCryptoProbeError("key version column inventory is empty")
    inventories: list[list[object]] = []
    for raw_table, raw_column in column_rows:
        table = _identifier(raw_table)
        column = _identifier(raw_column)
        version_rows = await _rows(
            connection,
            f'SELECT DISTINCT "{column}" FROM "{table}" '
            f'WHERE "{column}" IS NOT NULL ORDER BY "{column}"',
        )
        inventories.append([row[0] for row in version_rows])
    return _verify_version_inventory(inventories, crypto.key_versions)


async def _cipher_coverage(
    connection: AsyncConnection,
    crypto: CryptoService,
    spec: CipherSpec,
) -> ProbeCoverage:
    row_count = _count(
        await _rows(
            connection,
            f"SELECT count(*) FROM {spec.source_sql} "
            f"WHERE {spec.payload_expression} IS NOT NULL",
        ),
        "encrypted row",
    )
    sample_rows = await _rows(
        connection,
        f"SELECT DISTINCT ON ({spec.version_expression}) "
        f"{spec.version_expression},{spec.payload_expression},"
        f"{spec.verification_expression},{spec.expected_digest_expression} "
        f"FROM {spec.source_sql} WHERE {spec.payload_expression} IS NOT NULL "
        f"ORDER BY {spec.version_expression},{spec.order_expression}",
    )
    if spec.kind == "phone":
        verified_versions = _verify_phone_samples(
            [tuple(row[:3]) for row in sample_rows],
            crypto,
            table=spec.context_table,
        )
    elif spec.kind == "packed_text":
        verified_versions = _verify_packed_text_samples(
            [tuple(row[:3]) for row in sample_rows],
            crypto,
            domain=spec.domain,
            table=spec.context_table,
            column=(
                "callback_secret_enc"
                if spec.table == "callback_task"
                else spec.column
            ),
        )
    else:
        verified_versions = _verify_raw_payload_samples(sample_rows, crypto)
    if (row_count == 0) != (verified_versions == 0):
        raise RecoveryCryptoProbeError("historical ciphertext coverage is incomplete")
    return ProbeCoverage(row_count, verified_versions)


async def _probe_connection(
    connection: AsyncConnection,
    settings: Settings,
    crypto: CryptoService,
) -> ProbeResult:
    await connection.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )
    expected_audit_keys = {
        kind: decode_audit_context_key(settings.credential(secret_name))
        for kind, secret_name in AUDIT_KEY_NAMES.items()
    }
    audit_rows = await _rows(
        connection,
        "SELECT key_kind,key_material FROM audit_context_signing_key "
        "WHERE key_kind IN "
        "('principal','system:api','system:realtime','system:bulk') "
        "ORDER BY key_kind",
    )
    audit_count = _verify_audit_keys(audit_rows, expected_audit_keys)

    encrypted_column_rows = await _rows(
        connection,
        "SELECT c.relname,a.attname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p') "
        "AND a.attnum>0 AND NOT a.attisdropped AND a.attname ~ '_enc$' "
        "AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid) "
        "ORDER BY c.relname,a.attname",
    )
    encrypted_column_count = _verify_encrypted_column_inventory(
        encrypted_column_rows
    )
    version_column_count, referenced_version_count = await _version_inventory(
        connection, crypto
    )

    callback_version_mismatch = _count(
        await _rows(
            connection,
            "SELECT count(*) FROM callback_task WHERE callback_secret_enc IS NOT NULL "
            "AND callback_secret_key_version <> "
            "((get_byte(callback_secret_enc,0) << 8) "
            "+ get_byte(callback_secret_enc,1))",
        ),
        "callback secret version mismatch",
    )
    if callback_version_mismatch:
        raise RecoveryCryptoProbeError("callback secret version binding is invalid")

    coverage: dict[str, ProbeCoverage] = {}
    for spec in CIPHER_SPECS:
        coverage[spec.label] = await _cipher_coverage(connection, crypto, spec)
    encrypted_rows = sum(item.rows for item in coverage.values())
    verified_samples = sum(
        item.key_versions_verified for item in coverage.values()
    )
    sms_message_rows = coverage["sms_message.phone_enc"].rows
    if (encrypted_rows == 0) != (verified_samples == 0):
        raise RecoveryCryptoProbeError("historical ciphertext coverage is incomplete")
    return ProbeResult(
        counts=ProbeCounts(
            audit_context_keys=audit_count,
            encrypted_columns=encrypted_column_count,
            encrypted_rows=encrypted_rows,
            ciphertext_samples_verified=verified_samples,
            key_version_columns=version_column_count,
            referenced_key_versions=referenced_version_count,
            sms_message_rows=sms_message_rows,
        ),
        coverage=coverage,
    )


async def probe() -> ProbeResult:
    settings = get_settings()
    crypto = CryptoService.from_settings(settings)
    engine = create_async_engine(settings.database_owner_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                return await _probe_connection(connection, settings, crypto)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _output(status: str, result: ProbeResult) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "status": status,
            "counts": asdict(result.counts),
            "coverage": {
                label: asdict(item) for label, item in result.coverage.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _empty_result() -> ProbeResult:
    return ProbeResult(
        ProbeCounts(0, len(EXPECTED_ENCRYPTED_COLUMNS), 0, 0, 0, 0, 0),
        {label: ProbeCoverage(0, 0) for label in EXPECTED_ENCRYPTED_COLUMNS},
    )


def main() -> int:
    try:
        result = asyncio.run(probe())
    except Exception:
        print(_output("failed", _empty_result()))
        return 1
    status = (
        "performed" if result.counts.encrypted_rows else "not_applicable_empty"
    )
    print(_output(status, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
