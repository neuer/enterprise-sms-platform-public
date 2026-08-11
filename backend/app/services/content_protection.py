"""短信展示正文和上行回复的授权解密边界。"""

from __future__ import annotations

from app.services.crypto import CryptoService, EncryptionContext

REDACTED_CONTENT = "[redacted]"


def decrypt_batch_display_content(
    crypto: CryptoService | None,
    payload: object,
    batch_no: str,
) -> str:
    """解密已通过批次授权过滤的展示正文；历史清除值保持不可逆。"""

    if payload is None:
        return REDACTED_CONTENT
    if crypto is None:
        raise RuntimeError("batch display content crypto is unavailable")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("invalid protected batch display content")
    return crypto.decrypt_bound_packed_text(
        bytes(payload),
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id=batch_no.strip(),
        ),
    )


def decrypt_reply_content(
    crypto: CryptoService | None,
    payload: object,
    event_key: str,
) -> str:
    """解密已通过回复授权过滤的展示正文；历史清除值保持不可逆。"""

    if payload is None:
        return REDACTED_CONTENT
    if crypto is None:
        raise RuntimeError("reply content crypto is unavailable")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("invalid protected reply content")
    return crypto.decrypt_bound_packed_text(
        bytes(payload),
        EncryptionContext(
            domain="reply-content",
            table="reply_event",
            column="content_enc",
            object_id=event_key.strip(),
        ),
    )


def decrypt_template_content(
    crypto: CryptoService | None,
    payload: object,
    template_id: int,
) -> str:
    """解密已通过模板授权过滤的正文，密文绑定模板稳定 ID。"""

    if crypto is None:
        raise RuntimeError("template content crypto is unavailable")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("invalid protected template content")
    return crypto.decrypt_bound_packed_text(
        bytes(payload),
        EncryptionContext(
            domain="sms-template-content",
            table="sms_template",
            column="content_enc",
            object_id=str(template_id),
        ),
    )


def decrypt_template_name(
    crypto: CryptoService | None,
    payload: object,
    template_id: int,
) -> str:
    """解密与模板稳定 ID 绑定的名称。"""

    if crypto is None:
        raise RuntimeError("template name crypto is unavailable")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("invalid protected template name")
    return crypto.decrypt_bound_packed_text(
        bytes(payload),
        EncryptionContext(
            domain="sms-template-name",
            table="sms_template",
            column="name_enc",
            object_id=str(template_id),
        ),
    )
