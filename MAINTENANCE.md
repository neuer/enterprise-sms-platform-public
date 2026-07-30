# MAINTENANCE.md — 安全且高效的日常交付

平台已进入维护期，本文件是唯一日常流程入口。`AUTOPILOT.md`、`BOOTSTRAP.md`、
`TASKS.md` 与里程碑 G0/G1 只保留建设期历史；完整 G2 仅用于受保护变更、专项复验和
生产候选，不进入普通编码循环。

`PROGRESS.md` 只登记需要仓库外状态变化或操作者协调才能解除的活跃阻塞；普通提交、
PR、CI 和已完成状态不回填。PR 与 CI 事实以 GitHub 为准，发布不可变证据以生产变更单
和 release manifest 为准；无活跃阻塞时只保留明确空态。

## 日常开发

首次克隆只安装版本化 Hook：

```bash
scripts/install_git_hooks.sh
```

编码过程中直接运行与改动相关的 pytest/Vitest 或局部静态检查。准备提交时再执行一次：

```bash
scripts/dev_check.sh --changed
git add <本次文件>
git commit
git push -u origin <branch>
```

`dev_check --changed` 是提交前组件检查，不要求每次保存后运行，也不触发测试服务器部署。
推送时，`pre-push` Hook 会扫描工作区和即将公开的提交，只报告文件/规则而不回显命中内容。
owner 分支会自动创建 Draft PR；精确 push CI 成功后，自动化将同一 SHA 的 PR 改为 Ready
并请求 squash merge。required `ci-gate`、会话解决和冲突保护仍由 GitHub 强制，禁止
管理员绕过。合并完成后，自动化以一次性 tag 把后续 `ci-gate` 精确绑定到 squash merge
SHA：PR head tree 与原 `ci-gate` 证据完全吻合时直接复用，否则在合并提交上完整重跑。
普通手工 `workflow_dispatch` 始终完整运行。已合并的远端分支只有在仍指向原 head SHA
时才以 lease 删除；若分支已被推进则保留并失败关闭。

CI 按风险运行组件门禁；认证/授权/审计、加密与 PII、发送/厂商、迁移、部署和控制面等
受保护变更进入 G2 integration。人工、定时与生产候选继续执行完整门禁。

## 按需测试部署

测试部署与日常开发解耦。只有需要共享环境、浏览器或真实接口验收的功能才部署；纯文档、
纯测试和不改变行为的重构无需部署。默认等待 PR 合并后，更新精确的 `origin/main`：

```bash
git fetch origin
scripts/test_update.sh apply --ref origin/main
```

`apply` 已完成差异分类、必要 CI 验证、构建、远端切换、verify 和终态解析；成功退出即表示
远端返回 `state=verified`。需要在变更前预览分类时可运行
`scripts/test_update.sh plan --ref origin/main`，需要稍后查看非敏感状态时再运行
`scripts/test_update.sh status`，二者都不是每次部署的强制重复步骤。

首次使用测试部署时才准备本地连接文件和 GitHub CLI：

```bash
cp test-update.env.example .env.test-update
chmod 600 .env.test-update
gh auth status --hostname github.com
```

`.env.test-update` 只允许 `SMS_TEST_UPDATE_TARGET` 与 `SMS_TEST_UPDATE_PORT`，被 Git
忽略且必须为当前用户拥有的 0400/0600 普通文件。不得在仓库、命令历史或文档中记录真实
主机地址、账号、密钥或测试数据。鉴权失效时只用
`gh auth login --hostname github.com --web`；不得粘贴或长期导出 GitHub token。

### 测试服务器门禁

| 风险 | 测试部署 | 合并 |
|---|---|---|
| `web-only` / `backend-safe`、无迁移 | 合并后的 `origin/main` 按需 `apply` | push CI 成功后自动 Ready + squash |
| `high-risk` 或迁移/控制面 | `apply` 前必须有目标 commit 的精确 `ci-gate=success` | push CI 成功后自动 Ready + squash |
| 未知/破坏性迁移 | fail closed，拆分并单独评审 | 禁止 |

测试更新只构建受影响镜像，不重复组件测试或 G2；有迁移才创建密文 checkpoint。无迁移
切换/验收失败自动恢复旧 commit 与应用镜像并记录 `rolled_back`，绝不自动回退 schema。
`status` 是统一的只读诊断入口，只输出仓库、commit、test-update 与 vendor-test 的非敏感状态。
如果测试服务器基线不在当前公开仓库对象库，日常入口会失败关闭；不得向公开工作区添加
私有归档 remote、fetch 私有提交或生成私有对象包。此类跨历史迁移必须在隔离临时证据
仓库中另行设计、评审和执行，先建立新的公开基线，再恢复本文件的日常流程。

owner PR 的精确 push CI 成功后会自动改为 Ready 并请求 squash merge。此自动化只完成
仓库集成，不代表测试服务器已验证；按需测试更新只针对合并后的精确 `origin/main`。
服务器基线或保护状态异常时仍失败关闭。分支部署只作为明确的合并前验收例外；若分支已经
通过人工验证，且 squash 生成的新 `main` commit 与已验证分支 commit 的 tree 完全一致，
仍可运行：

```bash
scripts/test_update.sh promote --ref origin/main
```

该命令要求 `main` 的精确 `ci-gate=success`，只切换服务器 Git 身份，不重建镜像、不迁移
数据库；若 tree 不一致则失败关闭，必须重新执行 `apply`，需要预览时可先运行 `plan`。
每次成功的 `apply` 或 `promote` 都写入 GitHub `test` Environment 的 Deployment 记录。

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
