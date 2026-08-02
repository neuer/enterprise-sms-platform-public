# Resend 安全日报投递伴生容器

这个目录负责把已经通过 `render_security_daily_report.py` 校验的脱敏 JSON 日报发送到固定 Resend HTTPS 端点。日常配置全部在平台管理员的 `/security-daily` 页面完成，不需要手工维护 Resend secret 或收件人文件。

## 页面配置

先启动主 Compose 并完成数据库迁移。管理员进入“安全日报 → 配置邮件”：

1. 打开“启用安全日报”。
2. 粘贴 Resend API Key；首次保存必须填写，后续留空表示保持当前 Key。
3. 填写 1–3 个收件人，每行一个或用逗号分隔。
4. 点击保存，页面显示配置状态和收件人数，不回显 Key。

页面保存会更新 `security_daily_resend_api_key` 与 `security_daily_recipient`，并将 `resend.json` 原子同步到 `deploy/security-report-config`。该目录已加入 Git 和 Docker build context 忽略规则。

首次部署只需准备两个共享目录（不写入 Key）：

```bash
sudo install -d -o 10001 -g 10001 -m 0700 deploy/security-report-config deploy/security-report-control
```

Resend Key 应具备发送权限并绑定 `reports.neuer.cn`。发件域名和地址固定为：

- 域名：`reports.neuer.cn`
- 地址：由独立 mailer 固定配置（不在公开文档中回显）

## 启动 mailer

在仓库根目录执行：

```bash
docker compose -f deploy/security-report/docker-compose.yml --profile send build security-report-mailer
docker compose -f deploy/security-report/docker-compose.yml --profile send up -d security-report-mailer
```

mailer 以 UID 10001 非 root 运行，只读根文件系统、无 Linux capabilities、无入站端口。它只读取 UI 同步的 `/run/config/resend.json` 和脱敏 control 请求，通过 `api.resend.com:443/emails` 投递，再把不含正文、地址或 Key 的结果写回 control 目录。

主 Compose API 需要挂载 `./security-report-config` 和 `./security-report-control`；独立 mailer 只读前者、读写后者。日报生成任务仍由 bulk worker 每分钟检查，在上海时间 08:00 后消费前一自然日的脱敏快照；缺少快照时记录 `unavailable`，不会用 0 伪造指标。

## 证据采集器（一次性安装，之后自动运行）

日报数据不是 mailer 生成的。主机侧需要安装同一提交中的
`security-report-collector.service` 和 `.timer`；采集器只读取固定主机日志，写入
`security-report-control/incoming/YYYY-MM-DD.json`，只保留聚合计数与覆盖状态，不写入
日志原文、IP、账号、请求路径或平台凭据。它默认在 07:50（Asia/Shanghai）运行，供
08:00 的日报任务消费；Web/API、管理审计或运行态探针未接入时，报告会保持 `attention`
并在“证据范围”显示缺口，不会用零值冒充完整覆盖。

由授权运维在主机上安装一次：

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
2. 打开一条已生成日报，先使用“安全预览”确认内容，再点击“手动投递”。
3. mailer 处理后，页面投递状态更新为“已投递”或“投递失败”；失败可使用“重试投递”。
4. 检查 mailer 日志只包含报告日期和状态，不包含 Resend Key、收件地址、正文或 Resend 响应正文。

CI、G2 和快速更新使用 Fake/Mock 传输，不真实外发。真实首封测试必须由管理员在页面明确发起。
