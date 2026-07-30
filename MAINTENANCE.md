# MAINTENANCE.md — 安全且高效的日常交付

平台已进入维护期。日常开发以本文件为入口；`AUTOPILOT.md`、里程碑 G0/G1/G2 和
`BOOTSTRAP.md` 的建库步骤只用于历史建设或专项全量复验。

## 一条最短主路径

```bash
# 首次克隆只需执行一次
scripts/install_git_hooks.sh
cp test-update.env.example .env.test-update
chmod 600 .env.test-update

# 每次开发
scripts/dev_check.sh --changed
git add <本次文件>
git commit
git push -u origin <branch>
scripts/test_update.sh plan --ref origin/<branch>
scripts/test_update.sh apply --ref origin/<branch>
scripts/test_update.sh status
```

`.env.test-update` 只允许 `SMS_TEST_UPDATE_TARGET` 与 `SMS_TEST_UPDATE_PORT`，被 Git
忽略且必须为当前用户拥有的 0400/0600 普通文件。不得在仓库、命令历史或文档中记录真实
主机地址、账号、密钥或测试数据。

推送时，版本化 `pre-push` Hook 会扫描工作区和即将公开的提交，只报告文件/规则而不回显
命中内容；owner 分支会自动创建 Draft PR，CI 直接绑定该分支 commit 并按其相对 `main`
的完整差异运行，不依赖 Actions 创建 PR 后的递归事件。`ci-gate` 是 `main` 唯一 required
check，且绑定 GitHub Actions 应用。按变更范围只运行必要 job；高风险 PR 进入 G2 integration，
性能与 release-control 仅在相应路径变化时加入。人工、定时与生产候选仍执行完整 11 阶段。

## 测试服务器门禁

| 风险 | 分支测试部署 | 合并 |
|---|---|---|
| `web-only` / `backend-safe`、无迁移 | 完成定向本地检查即可直接 `apply`，CI 并行运行 | 等待 required `ci-gate` |
| `high-risk` 或迁移/控制面 | `apply` 前必须有目标 commit 的精确 `ci-gate=success` | 等待 required `ci-gate` |
| 未知/破坏性迁移 | fail closed，拆分并单独评审 | 禁止 |

测试更新只构建受影响镜像，不重复组件测试或 G2；有迁移才创建密文 checkpoint。无迁移
切换/验收失败自动恢复旧 commit 与应用镜像并记录 `rolled_back`，绝不自动回退 schema。
`status` 是统一的只读入口，只输出仓库、commit、test-update 与 vendor-test 的非敏感状态。

PR 通过分支环境验收后改为 Ready 并 squash merge。若 squash 生成的新 `main` commit 与
已验证分支 commit 的 tree 完全一致，运行：

```bash
scripts/test_update.sh promote --ref origin/main
```

该命令要求 `main` 的精确 `ci-gate=success`，只切换服务器 Git 身份，不重建镜像、不迁移
数据库；若 tree 不一致则失败关闭，必须重新走 `plan` / `apply`。每次成功的 `apply` 或
`promote` 都写入 GitHub `test` Environment 的 Deployment 记录。

## 生产发布

1. 最终 SHA 执行完整质量、安全与 G2 门禁，再构建四镜像；候选内容只执行一次 Trivy
   HIGH/CRITICAL 扫描。
2. 推送后按 RepoDigest 回拉，四个不可变 image ID 必须与候选逐一相同。
3. `release-gate` 绑定 VERSION、commit、Alembic head、OpenAPI SHA256、SBOM、workflow
   run、镜像身份及 attestation，并自动生成 `manifest.json`。
4. 目标主机执行 `release prepare`、`release activate`、`release status`；数据库只允许
   expand，备份、迁移头、健康、运行镜像和账本检查不得省略。

管理员初始化、正式厂商 Key 安装/轮换、测试号码管理和真实联调激活都是独立操作，绝不
夹带进代码发布。数据库、Docker volume、运行态目录和真实联调数据默认永久保留。
