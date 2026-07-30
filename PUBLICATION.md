# 公开仓库开发与发布流程

## 安全边界

本仓库以无私有历史的公开根提交开始，日常开发直接在公开仓库的短生命周期分支进行。
历史私有归档只作为受限恢复副本，不得添加为本仓库 remote，也不得 fetch、merge、
cherry-pick 或推送其中的任何 Git 对象。

本地 `.env`、`deploy/secrets/`、运行数据、日志与导出文件保留在工作区供开发使用，
但必须同时满足 Git ignore、本地提交/推送门禁和 CI 公开仓库门禁。任何门禁只报告
文件位置与规则，不回显命中值。

## 日常流程

1. 从最新 `main` 创建短生命周期分支。
2. 正常开发并运行与改动相关的测试。
3. 提交前运行 `python3 scripts/check_public_readiness.py`。
4. 推送分支；本地 Hook 只扫描新增提交，安全内容无需人工解锁。
5. 通过 Pull Request 合并；`main` 禁止直接推送、强推和删除。

## GitHub 设置

1. 默认分支要求 Pull Request、公开仓库门禁与会话解决。
2. Actions 默认权限为只读仓库内容，fork PR 不取得 secrets 或写权限。
3. 启用 secret scanning、push protection、Dependabot alerts 与私密漏洞报告。
4. CI 执行规格、不变量、公开仓库、SAST、依赖、secret 和配置检查。
5. Release、artifact、Pages、Packages 与 workflow 日志不得承载凭据、PII 或内部证据。

## 失败处理

推送前命中门禁时，先移除敏感内容并轮换可能受影响的凭据，再重新检查。若敏感内容已经
进入公开远端，立即将仓库设为私有、吊销凭据、隔离旧历史，并以新的无历史公开根重新
发布；追加“删除敏感内容”的提交不能清除旧对象。
