# Resend 安全日报投递伴生容器

> **生产 No-Go：** 当前伴生 mailer 仍是第五个本地 build 镜像，下面的目录默认落在 checkout，
> collector 也尚未纳入受控宿主资产升级，access log 没有 Runtime VMDK 容量门禁和 rotate/reopen
> 资产。正式发布、绝对 Runtime 路径、轮转和升级合同闭合前，禁止在生产执行本页的 build/up 或
> enable 命令；本页命令只用于开发/预生产修复验证。

这个目录负责把已经通过 `render_security_daily_report.py` 校验的脱敏 JSON 日报发送到固定 Resend HTTPS 端点。日常配置全部在平台管理员的 `/security-daily` 页面完成，不需要手工维护 Resend secret 或收件人文件。

## 页面配置

先启动主 Compose 并完成数据库迁移。管理员进入“安全日报 → 配置邮件”：

1. 打开“启用安全日报”。
2. 粘贴 Resend API Key；首次保存必须填写，后续留空表示保持当前 Key。
3. 填写 1–3 个收件人，每行一个或用逗号分隔。
4. 点击保存，页面显示配置状态和收件人数，不回显 Key。

页面保存会更新 `security_daily_resend_api_key` 与 `security_daily_recipient`，递增数据库中的配置版本，并将同一版本写入 `deploy/security-report-config/resend.json`。每个投递请求也绑定其创建事务读取到的配置版本；mailer 在建立任何网络连接前拒绝版本不一致的请求，避免数据库已删除的旧收件人因文件同步失败而继续收信。该目录已加入 Git 和 Docker build context 忽略规则。

从不含配置版本的旧版本升级后，现有 `resend.json` 会被安全拒绝。管理员必须在“配置邮件”中重新保存一次（Key 留空表示保持当前 Key），使文件获得当前版本；完成前日报投递会显式失败，不会退回使用无法证明新鲜的旧收件人配置。

首次部署只需准备两个共享目录（不写入 Key）：

```bash
sudo install -d -o 10001 -g 10001 -m 0700 deploy/security-report-config deploy/security-report-control
```

Web 容器访问日志落盘目录由 nginx（UID 101）写入，采集器以 root 只读聚合：

```bash
sudo install -d -o 101 -g 101 -m 0750 deploy/security-report-nginx
```

Resend Key 应具备发送权限并绑定 `reports.neuer.cn`。发件域名和地址固定为：

- 域名：`reports.neuer.cn`
- 地址：由独立 mailer 固定配置（不在公开文档中回显）

## 启动 mailer（仅开发/预生产修复验证）

在仓库根目录执行：

```bash
docker compose -f deploy/security-report/docker-compose.yml --profile send build security-report-mailer
docker compose -f deploy/security-report/docker-compose.yml --profile send up -d security-report-mailer
```

mailer 以 UID 10001 非 root 运行，只读根文件系统、无 Linux capabilities、无入站端口。它只读取 UI 同步的 `/run/config/resend.json` 和脱敏 control 请求，通过 `api.resend.com:443/emails` 投递，再把不含正文、地址或 Key 的结果写回 control 目录。

mailer 镜像不参与 CI 构建、test_update 快速更新（快速更新只覆盖 api/web 组件）和四个最终镜像的 Trivy 自动门禁。`deploy/scripts/send_security_daily_report_resend.py`、`deploy/scripts/render_security_daily_report.py` 或 `deploy/templates/security_daily_report.*` 变更后，必须在目标主机用上述命令手工重新 build 并重启该容器，平台快速更新不会代为完成。

主 Compose API 需要挂载 `./security-report-config` 和 `./security-report-control`；独立 mailer 只读前者、读写后者。日报生成任务仍由 bulk worker 每分钟检查，在上海时间 08:00 后消费前一自然日的脱敏快照；缺少快照时记录 `unavailable`，不会用 0 伪造指标。

自动投递：生成任务在 08:00 后每天只提交一次投递——正常报告直接发送；
证据不可用也会发送“问题通报”邮件（指标显式不可用并附原因），提醒收件人处理，
不会静默不发。发信配置不完整（缺少 Resend Key 或收件人）时物理上无法发信，
记录会显式标记投递失败并写明原因，恢复配置后自动补发。

记录保留：日报记录逐条保留，不覆盖。自动路径每天最多一条 `auto` 记录
（证据源从不可用恢复为可用时允许替换当天 auto 记录）；管理员“立即生成”每次
新增一条 `manual` 记录并立即投递，证据不可用同样新增记录并发送问题通报。
当天已有任意记录投递成功（含手动）时，自动路径不再重复投递。

worker-bulk 对 `security-report-control` 为可写挂载（用于提交投递请求），但
始终不挂载 Resend 配置目录。

## 证据采集器（当前仅开发/预生产修复验证）

日报数据不是 mailer 生成的。主机侧需要安装同一提交中的
`security-report-collector.service` 和 `.timer`；采集器只读取固定主机日志，写入
`security-report-control/incoming/YYYY-MM-DD.json`，只保留聚合计数与覆盖状态，不写入
日志原文、IP、账号、请求路径或平台凭据。它默认在 07:50（Asia/Shanghai）运行，供
08:00 的日报任务消费。

采集器当前覆盖的证据源：

- SSH 认证日志：Ubuntu Server 24.04 使用 `/var/log/auth.log`，采集器同时读取昨日
  轮转文件（`.1`、`.1.gz`），并匹配
  journald ISO、系统 syslog 与“Accepted key”等实际措辞；
- Fail2ban：`/var/log/fail2ban.log` 及轮转文件；
- Web/API access log：默认读取 `deploy/security-report-nginx/access.log`
  （nginx 以 UID 101 落盘），日志格式固定包含 `$time_local` 以便按上海自然日
  归属；文件缺失时回退读取平台 web 容器的 Docker stdout 日志；
- 运行态探针：读取 `/var/lib/docker/containers/*/config.v2.json` 聚合平台容器
  运行中/异常计数（一次性初始化容器不计入），不依赖 Docker CLI 或 socket。

管理审计由平台生成任务在入库前单独注入：`sms_send` 与 `sms_accept` 只读
`security_daily_audit_evidence` 视图（不含 before/after 载荷列；生成代码读取
时将 `ip` 列别名化为 `source_ip`），日报展示最近 10 条审计事件与总数，不读取
审计 JSON 明细。任一证据源缺失时，报告保持
`attention` 并在“证据范围”显示缺口，不会用零值冒充完整覆盖。

以下安装示例当前不得用于生产；只有上述生产 No-Go 闭合后，才可由授权运维按新的受控入口安装：

采集器只使用 Python 标准库，必须由生产宿主固定的 `/usr/bin/python3` 运行；该解释器
必须是 Python 3.12。它不依赖、也不得回退到发布目录中的 `backend/.venv`。启用 timer
前先完成生产宿主只读预检，并确认 Ubuntu 的 `rsyslog` 正在把 SSH 认证事件写入
`/var/log/auth.log`；文件缺失或目标日期不可归属时，日报保持“证据缺失”，不得改用
不受控路径或把缺失当作零事件。

```bash
sudo install -d -o root -g root -m 0750 \
  /opt/sms-platform/deploy/security-report-control/incoming
sudo install -m 0644 deploy/systemd/security-report-collector.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/security-report-collector.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now security-report-collector.timer
```

安装后不需要在页面维护采集器；页面只负责日报启停、Resend 配置、生成、预览和投递。
若 `incoming` 没有对应日期文件，“立即生成”会明确记录“证据不可用”，不会把示例 JSON
当作真实报告。

## 页面验收

1. `/security-daily` 概览显示“配置完整”、收件人数和固定发件地址。
2. 自动路径每天 08:00 后生成并发送一封（正常报告或问题通报）；打开日报详情可查看“自动/手动”来源。
3. 打开一条已生成日报，先使用“安全预览”确认内容，再点击“手动投递”可补发或重试。
4. mailer 处理后，页面投递状态更新为“已投递”或“投递失败”；失败可使用“重试投递”。
5. 检查 mailer 日志只包含报告日期和状态，不包含 Resend Key、收件地址、正文或 Resend 响应正文。

CI、G2 和快速更新使用 Fake/Mock 传输，不真实外发。真实首封测试必须由管理员在页面明确发起。
