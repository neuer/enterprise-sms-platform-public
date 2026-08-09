"""不可由业务数据库凭据伪造的事务级审计上下文签名。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def decode_audit_context_key(value: str) -> bytes:
    """审计上下文 key 必须是独立的 32 字节 base64 secret。"""

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RuntimeError("audit context key is invalid") from error
    if len(decoded) != 32:
        raise RuntimeError("audit context key is invalid")
    return decoded


def _field(value: str) -> str:
    return value.encode("utf-8").hex()


def canonical_audit_context(
    *,
    txid: int,
    database_user: str,
    correlation_id: str,
    subject_kind: str,
    actor_name: str,
    account_id: str,
    identity_id: str,
    app_id: str,
) -> bytes:
    """长度安全的固定字段顺序，与 PostgreSQL trigger 逐字一致。"""

    return "\n".join(
        (
            "v2",
            str(txid),
            _field(database_user),
            _field(correlation_id),
            _field(subject_kind),
            _field(actor_name),
            account_id,
            identity_id,
            app_id,
        )
    ).encode("utf-8")


def sign_audit_context(key: bytes, **values: str | int) -> str:
    """签名绑定事务 ID、数据库角色、关联 ID 与稳定主体。"""

    payload = canonical_audit_context(
        txid=int(values["txid"]),
        database_user=str(values["database_user"]),
        correlation_id=str(values["correlation_id"]),
        subject_kind=str(values["subject_kind"]),
        actor_name=str(values["actor_name"]),
        account_id=str(values["account_id"]),
        identity_id=str(values["identity_id"]),
        app_id=str(values["app_id"]),
    )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def canonical_system_audit_context(
    *,
    txid: int,
    database_user: str,
    correlation_id: str,
    producer_domain: str,
    actor_name: str,
    action: str,
) -> bytes:
    """绑定自治生产者与动作；系统 key 不能签名 human/api_app 主体。"""

    return "\n".join(
        (
            "system-v2",
            str(txid),
            _field(database_user),
            _field(correlation_id),
            _field(producer_domain),
            _field(actor_name),
            _field(action),
        )
    ).encode("utf-8")


def sign_system_audit_context(key: bytes, **values: str | int) -> str:
    """签名绑定事务、数据库角色、关联 ID、系统生产者与动作。"""

    payload = canonical_system_audit_context(
        txid=int(values["txid"]),
        database_user=str(values["database_user"]),
        correlation_id=str(values["correlation_id"]),
        producer_domain=str(values["producer_domain"]),
        actor_name=str(values["actor_name"]),
        action=str(values["action"]),
    )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()
