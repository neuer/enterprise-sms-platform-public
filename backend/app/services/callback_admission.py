"""发送准入里的未解决回调失败。

全量 ``callback_task.status='dead'`` 仍是历史与存储可见口径。
这里只排除由撤销事务写入的 ``CallbackConfigRevoked``；空原因和未知原因继续计入。
"""

UNRESOLVED_CALLBACK_DEAD_SQL = (
    "status='dead' AND last_error IS DISTINCT FROM 'CallbackConfigRevoked'"
)
