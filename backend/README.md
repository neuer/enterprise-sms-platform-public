# Backend

FastAPI、Celery 与厂商适配层代码目录。运行时按七个最小权限数据库职责隔离，迁移由 Compose 的 `migrate` 服务以 `sms_owner` 执行；见 `deploy/database-roles.md`。
