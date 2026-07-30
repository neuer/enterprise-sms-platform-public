# 公开仓库开发与发布流程

## 安全边界

本仓库以无私有历史的公开根提交开始，日常开发直接在公开仓库的短生命周期分支进行。
历史私有归档只作为受限恢复副本，不得添加为本仓库 remote，也不得 fetch、merge、
cherry-pick 或推送其中的任何 Git 对象。

测试服务器若仍停留在与公开根无共同历史的旧基线，`scripts/test_update.sh` 必须失败关闭。
日常入口不提供跨历史 cutover，也不得通过临时 ref、Git pack 或对象导入绕过。一次性基线
迁移只能在不属于本公开工作区的隔离临时证据仓库中完成，并经单独变更评审确认不会把
私有历史、ref、URL 或对象带回公开仓库；迁移完成后服务器应直接以公开 commit 作为新基线。

本地 `.env`、`deploy/secrets/`、运行数据、日志与导出文件保留在工作区供开发使用，
但必须同时满足 Git ignore、本地提交/推送门禁和 CI 公开仓库门禁。任何门禁只报告
文件位置与规则，不回显命中值。

## 日常流程

1. 从最新 `main` 创建短生命周期分支。
2. 首次克隆执行 `scripts/install_git_hooks.sh`；正常开发后运行
   `scripts/dev_check.sh --changed`。
3. 推送分支；版本化 Hook 同时扫描工作区与新增提交，安全内容无需人工解锁。
4. owner 分支自动创建 Draft PR；精确 push CI 成功后，自动化将同一 SHA 的 PR 改为
   Ready 并请求 squash merge，不使用管理员绕过。
5. `apply` / `promote` 前确认 `gh auth status --hostname github.com` 有效；只使用系统
   钥匙串/官方设备登录，不粘贴或长期导出 GitHub token。
6. required `ci-gate`、会话解决和冲突保护全部满足后由 GitHub 自动 squash merge；
   `main` 禁止直接推送、强推和删除。
7. 自动合并不代表测试环境部署成功；默认对合并后的精确 `origin/main` 重新执行
   `plan` / `apply` / `status`。若分支已提前验证且 tree 相同，才可用
   `scripts/test_update.sh promote --ref origin/main` 免重建提升。

## GitHub 设置

1. 默认分支要求 Pull Request、会话解决、线性历史和唯一 required `ci-gate`；required
   check 必须绑定 GitHub Actions 应用，禁止同名外部状态伪造通过。
2. Actions 默认权限为只读仓库内容；自动 Draft PR 工作流仅取得 PR 写权限，自动合并
   工作流仅在精确 push CI 成功后取得 contents/PR 写权限，并同时校验 owner、同仓分支和
   head SHA。fork PR 不取得 secrets 或写权限，自动合并禁止 `--admin` 绕过。
3. 启用 secret scanning、push protection、Dependabot alerts 与私密漏洞报告。
4. CI 执行规格、不变量、公开仓库、SAST、依赖、secret 和配置检查。
5. Release、artifact、Pages、Packages 与 workflow 日志不得承载凭据、PII 或内部证据。
6. 只允许 squash/rebase，自动删除已合并分支；`v*` 标签禁止改写或删除。

## 失败处理

推送前命中门禁时，先移除敏感内容并轮换可能受影响的凭据，再重新检查。若敏感内容已经
进入公开远端，立即将仓库设为私有、吊销凭据、隔离旧历史，并以新的无历史公开根重新
发布；追加“删除敏感内容”的提交不能清除旧对象。
