"""发送持久化错误的固定分类；异常原文、SQL 与参数不得进入日志或指标。"""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

SEND_DB_REASONS = frozenset(
    {"cas_lost", "db_deadlock", "db_timeout", "db_connection", "unexpected"}
)


class SendCasLost(RuntimeError):
    """已锁定事实之后 CAS 仍未命中，必须回滚整个事务。"""


class RolledBackSendDeadlock(RuntimeError):
    """数据库明确以 40P01 中止且事务上下文已退出，可重试原落库输入。"""


def send_sqlstate(error: BaseException) -> str | None:
    """仅提取驱动 SQLSTATE，不解析可能包含机密的错误文本。"""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for name in ("sqlstate", "pgcode"):
            value = getattr(current, name, None)
            if isinstance(value, str) and len(value) == 5 and value.isalnum():
                return value
        for name in ("orig", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


def send_db_reason(error: BaseException) -> str:
    """CAS、死锁、超时、连接故障与未知编程错误保持可区分。"""
    if isinstance(error, SendCasLost):
        return "cas_lost"
    state = send_sqlstate(error)
    if isinstance(error, RolledBackSendDeadlock) or state == "40P01":
        return "db_deadlock"
    if state in {"55P03", "57014"} or isinstance(
        error, (TimeoutError, asyncio.TimeoutError, SqlAlchemyTimeoutError)
    ):
        return "db_timeout"
    if (
        (state is not None and state.startswith("08"))
        or (isinstance(error, DBAPIError) and error.connection_invalidated)
        or isinstance(error, ConnectionError)
    ):
        return "db_connection"
    return "unexpected"
