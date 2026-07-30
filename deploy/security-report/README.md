# Resend 安全日报投递伴生容器

这个目录只负责把已经通过
`render_security_daily_report.py` 严格校验和脱敏的 JSON 日报发送到固定
Resend HTTPS 端点。它不采集或解析服务器日志，也不加入短信平台 API、worker、
beat 或固定八件生产 secrets。

## 固定安全边界

- 发信域名固定为 `reports.example.com`，发件地址固定为
  `security-daily@reports.example.com`。
- Resend Key 只从 Compose 的 `resend_api_key` Docker secret 读取，禁止放入
  `.env`、命令参数、日志、数据库或日报。
- 收件人从独立只读文件读取，每行一个、最多 3 个；仓库不保存真实收件地址。
- 容器以 UID 10001 非 root 运行，只读根文件系统、无 Linux capabilities、
  无入站端口。
- HTTPS 连接固定到 `api.resend.com:443` 的 `POST /emails`，不继承宿主代理、
  不跟随重定向，TLS 最低 1.2。
- 每个报告日期使用同一个 `Idempotency-Key`。429、5xx 或连接失败最多安全重试
  2 次，永久错误不重试，错误信息不包含 Resend 响应正文。
- CI、G2 和快速更新只允许 Fake/Mock 传输，绝不真实调用 Resend。

2026-07-26 已由信息安全负责人确认：脱敏安全日报正文在 Resend 美国区域保存
30 天符合公司信息安全要求。

## 首次配置

在这个目录执行。先准备不入 Git 的收件人文件：

```bash
cp config/recipients.example.txt config/recipients.txt
chmod 0600 config/recipients.txt
```

把示例地址改为经过批准的真实收件地址。然后从当前终端的标准输入原子安装
Docker secret 源文件。关闭 shell tracing，粘贴一次 Key 后按回车：

```bash
set +x
read -rsp 'Resend API Key: ' RESEND_API_KEY
printf '%s\n' "$RESEND_API_KEY" | \
  python3 ../scripts/install_resend_api_key.py \
  --output secrets/resend_api_key
unset RESEND_API_KEY
```

Key 不进入命令参数，安装器使用同目录临时文件原子替换并固定为 `0600`。Key
必须在 Resend 控制台设置为：

- Permission: `Sending access`
- Domain: `reports.example.com`

不要把 Key 粘贴到终端命令参数、聊天、Issue 或 PR。

日志解析器完成后，必须以原子替换方式生成
`runtime/current.json`；不能把未脱敏原始日志放进这个目录。

## 本地校验与单次发送

先构建镜像：

```bash
docker compose --profile send build security-report-mailer
```

默认 service command 会真实发送。要只校验输入且不联网，显式覆盖 command：

```bash
docker compose --profile send run --rm security-report-mailer \
  --input /reports/current.json \
  --recipients-file /run/config/recipients
```

确认报告日期、收件人数和 Resend 域名状态后，执行单次真实投递：

```bash
docker compose --profile send run --rm security-report-mailer
```

成功输出只包含报告日期，不显示收件地址、Key、邮件正文或 Resend 响应正文。
失败时先修复原因，再用同一报告日期重跑；幂等键会阻止重复投递。

## 上线前检查

1. `dig` 能查到 Resend 当前显示的 DKIM、MAIL FROM MX 和 SPF 三条记录，以及
   `_dmarc.reports.example.com` 的 `v=DMARC1; p=none;` 监控策略。
2. Resend Domains 页面显示 `reports.example.com` 为 `verified`。
3. API Key 是 Sending-only 且绑定上述域名。
4. `secrets/resend_api_key` 与 `config/recipients.txt` 权限均为 `0600`。
5. `runtime/current.json` 已通过本地只校验命令。
6. 首封测试邮件必须由操作者明确发起，并核对 HTML、纯文本回退与邮件头认证结果。

解析器和定时调度尚未接入前，不安装 systemd timer；先保持手工单次发送，避免空
报告或旧报告被每日重复触发。

DMARC 初始使用 `p=none`，只监控、不改变失败邮件处置。确认所有合法发信来源持续
通过 DMARC 后，再单独评审是否升级为 `quarantine` 或 `reject`；不要在首次投递时
直接收紧策略。
