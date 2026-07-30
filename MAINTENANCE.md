# MAINTENANCE.md — 日常开发、测试与发布

平台已完成从零建设。日常维护以本文件为入口；`AUTOPILOT.md`、里程碑 G0/G1/G2 和
`BOOTSTRAP.md` 的建库步骤只用于历史建设或专项全量复验，不要求每次改代码重走。

## 日常开发到测试服务器

1. 开发时只运行与改动直接相关的测试、类型检查或页面检查。
2. 提交并推送分支；GitHub 根据共享风险分类异步运行 backend、frontend、security
   和必要的 G2，不作为测试服务器更新的前置等待。
3. 推送完成后直接运行：

   ```bash
   scripts/test_update.sh --ref origin/<branch>
   ```

4. driver 不查询托管 CI，只构建受影响镜像，不重复 pytest、前端测试或 G2。远端达到
   `state=verified` 后，做本次功能对应的浏览器或 API 表面验收。

变更分三档：

| 档位 | 典型改动 | 测试环境处理 |
|---|---|---|
| 普通 | 页面、普通 API/业务逻辑 | 无迁移则不做数据库 checkpoint；只替换相关镜像 |
| 受保护 | 认证、授权、审计、加密/PII、发送/厂商链路 | CI 运行完整 G2；远端严格暂停并检查安全状态 |
| 迁移/控制面 | Alembic、Compose、host-control | expand-only；有迁移才创建密文 checkpoint；控制面需同 commit 快照 |

普通无迁移更新在切换或验收失败时自动恢复上一版镜像并记录 `rolled_back`；不回退数据库
schema。迁移、受保护状态异常或自动恢复失败仍保持 fail closed。所有路径始终保留
PostgreSQL、Docker volume、运行态目录和真实联调数据。

## 生产发布

1. 最终 SHA 重新执行完整质量、安全与 G2 门禁，再构建四镜像；候选内容只执行一次
   Trivy HIGH/CRITICAL 扫描。
2. 推送镜像后，以最终 RepoDigest 回拉，要求四个不可变 image ID 与候选报告逐一相同；
   不再对同一内容重复扫描。
3. release-gate 报告绑定 VERSION、commit、Alembic head、OpenAPI SHA256、四份 SBOM、
   workflow run 与镜像身份，并归档 attestation。用 `scripts/create_release_manifest.py`
   从最终证据生成 `manifest.json`，清单必须绑定报告 SHA256，禁止跨提交复用或手写 JSON。
   只有 PostgreSQL/Redis 镜像实际变化时才运行数据镜像门禁；只有 PostgreSQL 变化时才
   要求备份变更和恢复演练证据。
4. 目标主机执行 `release prepare`、`release activate`、`release status`。数据库变更仍只
   允许 expand；生产备份、迁移头、健康、运行镜像和账本检查不省略。

管理员初始化、正式厂商 Key 安装/轮换、测试号码管理和真实联调激活都是独立操作，绝不
夹带进代码发布。
