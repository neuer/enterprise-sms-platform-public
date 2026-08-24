# docs/UAT.md — 上线验收用例集 v1.6

> 自动化执行环境：预生产（VENDOR_MOCK=1、AUTH_MOCK=1、告警渠道为空的 log-sink；mock 契约见 vendor-api.md 第3节）。任何自动化用例不得请求真实 LDAP、厂商、企微或 SMTP。
> 每例记录：执行人 / 日期 / 结果（P/F）/ 截图或日志编号，汇总到受限测试归档；真实
> 号码、内部证据与报告不进入公开仓库。
> 角色账号：admin01 / approver01 / operator01 / viewer01（seed-dev mock 用户，密码从本机 0600 `ldap_bind_password` secret 读取）+ 应用 app-iam(verify)、app-oa(notice)、app-mkt(market)。Phase 0 真人 UAT 在 AUTH_MOCK=0 的预生产环境以四个本地账号重复 01–04；AD 登录与角色映射仅在 AD Provider 后续启用前另行复验。
> 用例临时修改 sys_config 时，必须先保存原值并在 finally 阶段恢复；失败退出也必须恢复，禁止污染后续用例。

| # | 用例 | 步骤要点 | 预期 | 映射 |
|---|---|---|---|---|
| 01 | 登录与角色权限 | Phase 0 用四个本地账号分别登录；AD 启用前再用四个目录账号复验 | 各自菜单与数据权限正确（附录A 可见性），Provider 不自动回退 | FR-3章 |
| 02 | 账号锁定 | operator01 连续错密 5 次 | 第 6 次提示锁定，15min 后可登录 | 3章 |
| 03 | 登录 IP 限流 | 同 IP 5min 内错 20 次（脚本） | 返回 429，IP 封 15min，正确密码也被拒 | 3章 |
| 04 | 强制下线 | admin 对 operator01 执行 revoke | 其已登录会话下一请求 401 | FR-18a |
| 05 | API 发送+幂等 | app-oa 同 biz_id 提交 2 次 | 第 2 次 idempotent=true 且批次号相同，只发一次 | FR-01 |
| 06 | 类别越权 | app-iam(仅verify) 发 market | 403 CATEGORY_NOT_ALLOWED | FR-00 |
| 07 | verify 频控 | 同号码 1min 内 2 条 verify | 第 2 条被剔，removed_freq_limit=1 | FR-11a |
| 08 | market 时间窗 | 21:30 提交 market | status=scheduled，次日08:00，deferred_reason=market_window，可取消 | FR-00 |
| 09 | 退订语自动追加 | market 内容不含退订语 | 下发内容尾部含"回T退订"，计费长度含之 | FR-00 |
| 10 | 营销同意留痕 | Web market 不勾选提交 | 422 CONSENT_REQUIRED；勾选后审计含 consent 记录 | FR-02 |
| 11 | 审批阈值与回避 | operator01 发 60 号码 market；operator01 兼 approver 审自己 | 进入待审批（阈值50）；自审 403，approver01 可通过 | FR-04 |
| 12 | 审批过期 | 保存原值，将审批过期临时改为5s，造一单后触发扫描，finally恢复 | status=expired，配额回补；log-sink 的 alert_log/通知记录存在 | FR-04 |
| 13 | 计费条 | 150 字内容发 100 号码 | est_segments=3，quota_cost=300，配额扣减一致 | FR-00a |
| 14 | 黑名单/敏感词 | 名单号码+敏感词内容各一批 | 分别 removed_blacklist 计数 / 422 SENSITIVE_WORD | FR-09/10 |
| 15 | 定时取消 | 定时批次到点前取消 | status=cancelled，配额回补 | FR-03 |
| 16 | 余额熔断与恢复 | mock 设 next_send_code=999 | 批次 balance_blocked、双队列停、alert_log 有crit且无外呼；/queue/resume 恢复续发 | FR-05 |
| 17 | uncertain 修复 | mock 注入 latency 12s | chunk=uncertain 不重发；reconcile 比对 raw 后修复 submitted，无重复下发 | FR-05 |
| 18 | 失败重发 | 用魔法号码 1990000* 产生 failed 后一键重发 | 新批次 resend_of 正确，走完整管控 | FR-05a |
| 19 | 模板全流程 | 建模板{1}{2}+var_specs→厂商申请→通过→带参发送 | BindTemplate 收到 {s10}{s6} 格式；参数超长 422 | FR-12 |
| 20 | 回调验签+重推 | callback_url指向mock sink；临时将重试间隔改为1,1,1,1,1，mock前5次500，finally恢复 | 5 次重试→dead→alert_log；手动重推成功；mock保存的raw body验签通过，±300s外测试请求被拒 | FR-07a |
| 21 | 号码搜索与解密审计 | viewer 搜某号码；admin 详情页解密 | viewer 仅本部门+mask；解密动作出现在审计 | FR-16/22 |
| 22 | 导出 decrypted | approver 导出明文 10 万行内 | 异步完成可下载；审计含 decrypted=true | FR-16 |
| 23 | 双 Key 轮换 | rotate 后新旧 Key 各发一次；到宽限期后旧 Key 再试 | 宽限期内均 200；到期旧 Key 401 | FR-18 |
| 24 | OTP 打码 | verify 发送后查库与详情页 | content 中验证码为等长 * ；下发原文正确（mock 侧核对） | FR-22a |
| 25 | unmatched 对账 | mock enqueue_report 注入陌生 customId 报告 | 落 unmatched_report（三列加密），ops 页可按号码查询并创建密文导出 | FR-06/10章 |
| 26 | 发送量异常告警 | mock 对 app-iam 灌 verify 突增（>基线×3 且≥500） | alert_log 有crit，文案含"核查来源/停用Key"建议；同日不重复且无外呼 | FR-25 |
| 27 | 任务健康与心跳 | docker stop beat 容器，等 2×最短任务间隔 | job_stalled 告警触发；恢复后仪表盘任务格转绿；手动触发按钮可用并记审计 | FR-27 |
| 28 | 号码时间线 | 对同一测试号发 notice、verify，再用 mock enqueue_reply 注入其回复 | 时间线按时序合并三事件，verify 内容已打码，回复右缩进标"↩ 用户回复"，徽标显示近30日量 | FR-26 |
| 29 | 应用 IP 白名单 | admin 将测试应用 allowed_ips 设为仅办公室出口 CIDR；分别从白名单内/外来源调用 API 发送，再清空恢复 | 白名单内 200；白名单外 403 IP_NOT_ALLOWED 且不消耗限流/配额；清空后任意来源 200 | FR-18 |

附加抽查（不计入 28 例，DB/存储/日志层）：七个职责角色均非 owner/超级用户且 audit 非 INSERT 操作被拒，callback/metrics 越权写入被拒，旧 sms_app 停用；raw_vendor_log 只有 payload_enc/custom_ids 无 phone JSON；callback_task 无 phone/body；导入逐号三列；decrypted 导出磁盘文件不可 grep 出 11 位号码；全量日志无 11 位连续手机号；stat_daily 计费条与当日批次 quota_cost 汇总一致。
