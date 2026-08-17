# 厂商主备出口 IP 双报备手册

## 目标与红线

智慧信息厂商按公网出口 IP 校验请求；未报备会返回 1010。主节点和冷备节点必须在 T0 前以**同一份申请**报备主出口 IP、备出口 IP，避免故障切换后被厂商拒绝。报备与验证不得调用 GetReport/GetReply：两接口拉走即消费，任何探针都会破坏生产报告所有权。

SecretName、SecretKey 均按平台 secrets 管理。报备工单使用合同客户编号或厂商提供的非密钥账户标识；**禁止**把 SecretKey、SecretName、API Key、回调 secret 或任何短信/手机号附在工单、邮件或聊天中。

## 报备前网络确认

由网络负责人分别在主、备节点确认：

- 生产流量经固定 SNAT/EIP 出口，容器重启、节点重启和路由切换后地址不漂移。
- 主出口 IP 与备出口 IP 不相同且均为厂商可识别的公网 IPv4；若经过双层 NAT，以厂商实际看到的最外层地址为准。
- 到厂商主/备域名的 TCP 443、DNS 和证书链可用；不允许绕过 TLS 校验。
- 备用节点默认不取得事件发布或报告拉取权，只有正式切换流程才启动 outbox-dispatcher/worker/beat。

公网 IP 值属于网络配置，可写入受控报备单，但不得写入 Git。本手册只保留字段名。

## 同单报备模板

一份报备必须同时包含：

| 字段 | 要求 |
|---|---|
| 合同客户编号 | 非 SecretName/SecretKey 的厂商客户标识 |
| 环境 | 生产；测试环境另单，不混用白名单 |
| 主出口 IP | 网络团队签字确认的固定公网地址 |
| 备出口 IP | 冷备节点固定公网地址 |
| 生效窗口 | 必须早于 T0 验证窗口 |
| 真实 QPS | 厂商书面确认账户总 QPS；回填 `sys_config.vendor_qps` |
| 单次号码上限 | 厂商书面确认；回填 `sys_config.vendor_batch_size` |
| 主备域名/地址 | 厂商书面确认及切换策略 |
| 联系人 | 厂商、网络、安全、平台值班人及升级电话 |

只有收到厂商**书面回执**，明确列出两 IP、QPS、单次号码上限、生效时间和工单号，才可进入验证。电话口头确认或“已收到”不算完成。

## 主备逐点验证

两个节点使用同一发布版本和各自本地 0600 secrets，生产必须 `ENVIRONMENT=production DEBUG=0 AUTH_MOCK=0 VENDOR_MOCK=0 REDIS_HA_MODE=managed`。验证窗口只额外启动单个 `worker-realtime`，不要在备节点启动 outbox-dispatcher、beat 或其他轮询 worker；API 不挂载厂商凭据，也不得作为出站探针。

在主节点执行一次平台内置 GetBalance 链路：

```bash
sudo /usr/local/sbin/sms-compose exec -T worker-realtime \
  python -c 'from app.tasks.poll_balance import poll_balance; raise SystemExit(0 if poll_balance() == 1 else 1)'
sudo /usr/local/sbin/sms-compose exec -T postgres \
  psql -U sms_owner -d sms -Atc \
  "SELECT status FROM job_run WHERE job_name='poll_balance' ORDER BY id DESC LIMIT 1"
```

期望命令退出 0 且最新状态为 success。按同一命令在备节点执行，再停止备节点 `worker-realtime`。GetBalance 不消费报告；禁止把命令替换成 GetReport/GetReply。

在经审批的厂商测试窗口，可从主、备各发送一条到专用测试号码，确认没有 1010；测试号码和正文不得写入执行日志。真实发送前必须确认短信签名、模板和合规审批，不以网络验收为由绕过业务规则。

## 1010 与切换阻断

- 任一请求返回 1010，视为 IP 校验失败：平台应产生 crit 告警、**不暂停队列**；不得重试轰炸厂商。
- 立即记录发生节点、时间、关联 job_run/alert_log ID 和厂商 request trace（若不含 PII）；不记录 SecretKey 或请求体。
- 网络团队复核实际 SNAT 地址，厂商按原工单核对白名单。两方书面确认修复后，先 GetBalance，再继续发送。
- 主节点成功而备节点 1010 时，冷备切换必须阻断；不得为了满足 RTO 临时直连未报备地址。
- mock 环境必须保留 1010→crit alert_log 的自动化验收；生产不通过故意撤销白名单制造故障。

## 开发测试环境的受控例外

正式 Key 在开发测试服务器首次联调时，按已确认的测试约定**默认按已报备处理**，不额外发送探测短信。唯一无消费预检是 GetBalance；若收到 1010，立即停止后续真实发送并 crit 告警（1010 本身不进入双队列熔断暂停），并把安全错误码告知操作者，由网络与厂商核对实际出口。此例外只适用于受控真实联调，不放宽上述生产主备书面报备和 T0 签收要求。完整步骤见 [真实厂商受控联调手册](../docs/runbooks/controlled-real-vendor-test.md)。

该例外环境的日常操作只允许在系统配置页完成：凭据密封安装、测试号码维护、激活、暂停、恢复和单号码 UAT 均不得改用 TTY 参数、环境变量或直接 Compose。部署不得自动激活真实联调；发布和 `vendor-control-agent.service` 启动后必须保持 inactive，待管理员在页面复核安全状态并再次明确授权后手工激活。

手机访问正式凭据表单所用的 Cloudflare Quick Tunnel 只是浏览器 HTTPS 传输入口，仅限开发
测试且无 SLA；它不代理应用到运营商的厂商请求，也不改变本手册中的厂商出口 IP、1010
处理或 GetBalance 预检边界。临时入口最多 15 分钟，HTTP 入口隐藏正式 Key 字段且不得降级。
入口启动、停止、一次性安装和残留检查见 [部署索引](README.md)；任何临时 URL、Key、测试
号码或登录信息都不得写入出口报备单。

## 上线与变更复核

T0 前将厂商回执工单号、两节点 GetBalance 验证时间、QPS/号码上限配置快照纳入发布记录。每季度冷备演练先复核两 IP 仍在白名单；任何 NAT、专线、防火墙、云账号或节点迁移变更都必须重新同单报备主备地址，并在变更后重复逐点验证。

全部应用迁移完成并按 PRD 第 10 章重置厂商密钥时，只轮换 secret 文件，不应删除已报备的备出口 IP。若厂商只允许单 IP，属于上线关键阻塞，必须升级安全负责人，不得静默降级为无冷备。
