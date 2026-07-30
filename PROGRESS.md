# PROGRESS.md — 当前维护状态

日常开发与交付以 `MAINTENANCE.md` 为权威入口；本文件只记录当前状态和仍需处理的阻塞，
不再沿用 M0–M4 建设期的续跑任务。

- 建设里程碑：M4.4 `DONE`；当前阶段为维护期，公开仓库安全边界、选择性 CI、owner PR 自动合并、测试服务器快速更新和生产发布门禁均已固化。
- 最近公开基线：`main` 的 PR #3、#4 已依次 squash merge；对应精确 CI、G2 与 CodeQL 均通过，青鸾版已成为唯一前端。
- 当前任务：T4.24 已完成；owner 同仓分支的精确 push CI 成功后，自动化将同一 SHA 的 PR 改为 Ready 并请求 squash auto-merge，不绕过 required checks、会话解决或冲突保护。
- 活跃 BLOCKED：测试服务器仍在当前公开仓库对象库不存在的旧 commit。日常
  `scripts/test_update.sh plan/apply` 会按设计失败关闭；当前公开工作区未导入、也不得导入
  私有归档 remote、ref、commit 或 Git pack，服务器数据与 volume 未因本轮文档修复变更。
- 下一步：本次自动合并工作流首次引入仍按既有流程完成合并；进入 `main` 后，后续 owner
  PR 无需人工转 Ready 或点击合并。跨历史测试服务器基线迁移继续另立变更单，在公开
  工作区之外的隔离临时证据仓库中设计、评审和执行。
