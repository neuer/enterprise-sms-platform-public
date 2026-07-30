# PROGRESS.md — 当前维护状态

日常开发与交付以 `MAINTENANCE.md` 为权威入口；本文件只记录当前状态和仍需处理的阻塞，
不再沿用 M0–M4 建设期的续跑任务。

- 建设里程碑：M4.4 `DONE`；当前阶段为维护期。`AUTOPILOT.md`、`BOOTSTRAP.md` 与
  `TASKS.md` 仅保留历史，日常开发、按需测试部署和生产发布统一从 `MAINTENANCE.md` 进入。
- 最近公开基线：`main` 的 PR #3–#7 已依次 squash merge；对应分支的精确 CI 均通过，
  青鸾版已成为唯一前端，维护流程已收敛，owner PR 自动合并及 merge SHA 主干验真已启用。
- 当前维护重点：开发与测试部署已经解耦；测试部署默认针对合并后的精确 `origin/main`，
  `apply` 负责最终 `state=verified` 校验，`plan` 与独立 `status` 只在需要时使用。
- 活跃 BLOCKED：测试服务器仍在当前公开仓库对象库不存在的旧 commit。日常
  `scripts/test_update.sh plan/apply` 会按设计失败关闭；当前公开工作区未导入、也不得导入
  私有归档 remote、ref、commit 或 Git pack，服务器数据与 volume 未因本轮文档修复变更。
- 下一步：跨历史测试服务器基线迁移继续另立变更单，在公开工作区之外的隔离临时证据
  仓库中设计、评审和执行；完成前，纯文档、纯测试和无需共享环境验收的维护工作不绑定
  测试服务器更新。
