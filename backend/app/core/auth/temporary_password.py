"""临时凭据期限的统一数据库表达式；缺失或越界策略失败关闭。"""

TEMPORARY_PASSWORD_EXPIRY_SQL = """(
    SELECT clock_timestamp() + make_interval(hours => value::integer)
    FROM sys_config
    WHERE key='local_temporary_password_ttl_hours'
      AND value ~ '^[0-9]{1,3}$'
      AND value::integer BETWEEN 1 AND 168
)"""
