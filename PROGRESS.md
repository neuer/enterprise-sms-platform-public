# PROGRESS.md — 活跃外部阻塞

日常开发与交付以 `MAINTENANCE.md` 为准。本文件只登记需要仓库外状态变化或操作者协调
才能解除的活跃阻塞；普通提交、合并结果和已完成状态不回填。无阻塞时只保留明确空态。

## TEST-BASELINE — 测试服务器公开基线迁移

- 影响：测试服务器当前基线不在公开仓库对象库中，`scripts/test_update.sh plan/apply`
  会按设计失败关闭；本地开发、CI 以及无需共享环境验收的维护工作不受影响。
- 失败关闭边界：不得向公开工作区导入私有归档 remote、ref、commit 或 Git pack，
  也不得借迁移改动服务器数据库、Docker volume 或运行态目录。
- 解除条件：另立变更单，在公开工作区之外的隔离临时证据仓库中完成设计、评审和迁移，
  再对精确 `origin/main` 正常执行 `apply`；只有返回 `state=verified` 后才删除本条阻塞。
