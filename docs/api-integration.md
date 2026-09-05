# 企业短信平台 API 接入文档（应用侧）

本文面向需要在自身业务系统中调用短信发送能力的接入方（内部系统/第三方应用）。
接口完整契约以仓库根目录 `openapi.yaml` 为准，本文是接入说明与常用场景手册。

## 1. 接入概览

接入方通过平台分配的一对 API Key 调用统一发送接口，厂商密钥、签名、模板同步、
报告轮询与对账全部由平台侧完成。接入方只需要：

1. 由平台管理员创建"应用"并领取一次性明文 API Key；
2. 按需配置：允许类别（verify/notice/market）、每日配额、每分钟限流、
   来源 IP 白名单、默认签名、结果回调；
3. 在自己的系统中携带 `X-Api-Key` 请求头调用接口；
4. 用返回的 `batch_no` 查询发送状态，或接收平台回调。

### 1.1 环境地址

| 环境 | 请求根地址 | 说明 |
|---|---|---|
| 测试环境 | `http://<测试服务器>:18080/api/v1` | 受控真实联调模式下普通发送被保护性拦截（见 9.4），仅 Mock 环境可完整测试 |
| 生产环境 | `https://<生产域名>/api/v1` | 由平台方提供，仅 HTTPS |

所有请求与响应均为 `application/json; charset=utf-8`。

### 1.2 名词

| 名词 | 含义 |
|---|---|
| 应用（App） | 平台 API 接入方身份，每个应用一对 API Key |
| 批次（Batch） | 一次发送请求对应的唯一记录，返回 `batch_no` |
| 消息（Message） | 批次内单个手机号的发送明细 |
| 计费条（Segment） | 计费单位：最终内容（含签名与退订语）≤70 字为 1 条，超过按 67 字/条向上取整 |
| `biz_id` | 接入方自有的业务幂等键；即时发送默认保护 24 小时，定时为 `scheduled_at+7` 天 |

## 2. 认证与密钥

### 2.1 请求头

```http
X-Api-Key: <你的 API Key>
```

Key 只通过请求头传递，不使用 Cookie、URL 参数或请求体。

### 2.2 密钥生命周期

- **一次性明文**：创建/轮换应用时平台只返回一次明文 Key，之后无法再次查询；
- **轮换**：管理员执行轮换后生成新 Key，旧 Key 进入宽限期（默认 72 小时）内仍有效，
  新旧 Key 可平滑并行，避免切换停机；
- **提前作废**：可立即作废旧 Key（宽限期内旧 Key 即刻失效）；
- **停用应用**：当前 Key 与旧 Key 全部立即失效，存量数据保留；
- **安全要求**：Key 应保存在服务端密钥管理系统，禁止写入前端代码、日志、截图、
  聊天记录；人员变更时应立即轮换。

### 2.3 来源 IP 白名单（推荐启用）

管理员可为应用配置 `allowed_ips`（IP 或 CIDR 列表，空=不限）。启用后：

- 请求来源 IP 不在白名单 → `403 IP_NOT_ALLOWED`，且不消耗限流与配额；
- 平台识别的是**接入方服务器的出口公网 IP**。请确认 NAT/代理后的稳定出口 IP，
  出口 IP 变化前先在平台侧更新白名单；
- 白名单配置修改立即生效，新旧 Key 受同一白名单约束。

## 3. 发送短信

### 3.1 请求

```http
POST /api/v1/messages/send
X-Api-Key: <你的 API Key>
Content-Type: application/json
```

请求体字段：

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `category` | string | 是 | `verify` / `notice` / `market` | 消息类别，决定队列、时间窗、频控与黑名单策略 |
| `mobiles` | string[] | 是 | 1–10000 个，格式 `^1\d{10}$` | 手机号列表 |
| `content` | string | 二选一（不推荐） | 1–500 字符 | 直接内容；会进入服务商人工审核流程，发送延迟大，正式接入必须改用模板 |
| `template_id` | int | 二选一（必须） | 已审核模板 | 全局平台模板 ID（非厂商编号）；正式接入统一使用模板发送 |
| `template_params` | string[] | 随模板 | 参数个数与模板一致 | 按 `{1}..{n}` 顺序替换 |
| `sign_name` | string | 否 | ≤32 字符 | 覆盖默认签名；未传使用应用默认签名 |
| `scheduled_at` | string | 否 | ISO8601 含时区 | 定时发送；如 `2026-08-05T10:00:00+08:00` |
| `biz_id` | string | 是 | 1–32 字符，稳定业务主键 | 业务幂等键；同一应用同键同指纹重放返回原批次，同键不同指纹返回 409。见 3.4 |

> ⚠ **发送方式要求**：正式接入必须使用已审核模板（`template_id` + `template_params`）。
> 直接内容（`content`）提交后会进入服务商的人工审核流程，发送延迟可能从数分钟到数小时；
> 仅在紧急且平台方同意的例外场景使用。模板在厂商侧审核通过（`approved`）后，
> 按模板发送全程自动、无需人工介入。

模板发送示例（正式接入标准用法）：

```json
{
  "category": "notice",
  "mobiles": ["138****8000"],
  "template_id": 12,
  "template_params": ["张三", "123456"],
  "biz_id": "ORDER-20260804-001"
}
```

（不推荐）直接内容示例——仅紧急例外：

```json
{
  "category": "notice",
  "mobiles": ["138****8000"],
  "content": "您的工单 #1024 已创建，请及时处理。",
  "biz_id": "ORDER-20260804-002"
}
```

### 3.2 响应

`200 OK`：

```json
{
  "batch_no": "12cabc53f9334095bca0adbb3d1e2d1e",
  "idempotent": false,
  "accepted": 1,
  "removed_duplicate": 0,
  "removed_blacklist": 0,
  "removed_freq_limit": 0,
  "est_segments": 1,
  "quota_cost": 1,
  "status": "queued",
  "deferred_reason": null,
  "scheduled_at": null
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `batch_no` | 批次号，后续查询状态的唯一凭证，请持久化保存 |
| `idempotent` | 本次是否命中幂等（`true` 表示返回的是历史批次，未重复发送） |
| `accepted` | 实际受理号码数（去重、黑名单、频控剔除后） |
| `removed_duplicate` / `removed_blacklist` / `removed_freq_limit` | 各环节剔除数 |
| `est_segments` | 单号码预估计费条 |
| `quota_cost` | 本批扣减计费条 = `est_segments × accepted` |
| `status` | 批次权威状态。首次受理多为 `queued`/`scheduled`；安全重放返回原批次当前状态，包括 `sending`、`completed`、`completed_unknown`、`cancelled`、`balance_blocked`。`completed_unknown` 表示存在未知结果，禁止自动重发 |
| `deferred_reason` | `market_window` 表示营销时间窗外自动转为次日窗口起点 |
| `scheduled_at` | 实际计划发送时间（定时或顺延后） |

### 3.3 类别策略

| 策略 | verify 验证码 | notice 通知 | market 营销 |
|---|---|---|---|
| 队列 | realtime（高优先） | realtime | bulk |
| 发送时间窗 | 不限 | 不限 | 默认 08:00–21:00，窗外自动转定时 |
| 黑名单 | 不拦截 | 默认拦截（应用可关） | 强制拦截 |
| 号码级频控 | 同号码 1 条/分钟、10 条/天（全局跨应用） | 默认不限 | 同号码同应用 1 条/天 |
| 审批 | 不适用 | 仅 Web 渠道触发 | 仅 Web 渠道触发 |
| 退订语 | — | — | 缺失时自动追加（应用可配置） |

注意：**API 渠道发送不触发审批**；营销短信在时间窗外提交会得到
`status=scheduled`、`deferred_reason=market_window`，可在发送前取消。

### 3.4 幂等与重试（重要）

`biz_id` 必须是业务事件的稳定不可复用标识（UUID 或业务单号），**禁止**按日/按周期
循环短序号。作用域是同一应用（API）或同一稳定账号（Web）。

保护窗口：

- 即时发送：`idempotency_record.expires_at = now()+24 hours`，Redis 索引 TTL 同样 24 小时；
- 定时发送：`scheduled_at + 7 days`；
- 窗口内相同请求返回原 `batch_no` 且 `idempotent: true`，不同请求体返回 409
  `IDEMPOTENCY_CONFLICT`；
- 响应头 `Idempotency-Expires-At` 与可选字段 `idempotency_expires_at` 给出截止时间；
- 过期后若批次仍处于
  `pending_approval/scheduled/queued/sending/balance_blocked`，或仍有
  `uncertain` / `unknown_terminal` 分片，或仍有未完成 callback
  （`pending`/`retrying`），同一 `biz_id` **仍不可复用**；
- 仅当记录已过期且批次已真正终态（无未知分片、无未完成 callback）时，同键才会
  删除旧记录并创建新批次。

推荐从稳定业务主键生成，例如 `{业务系统}-{业务类型}-{业务单号}-{动作版本}`。
网络超时、客户端超时和 5xx 重试必须复用原 `biz_id`；只有业务语义变化时才换新键。
不要把手机号、短信正文写入 `biz_id`，不要只用时间戳，也不要用平台 `batch_no` 反向替代首次请求所需的业务 ID。

正确示例：

```json
{
  "biz_id": "order-20260904-000123",
  "mobiles": ["138****8000"],
  "content": "您的工单已创建"
}
```

错误示例：

1. 缺少 `biz_id`：API 返回 400 `INVALID_PARAM`，错误说明缺少必填业务幂等键；
2. 使用固定常量（如 `test`）：后续真实业务会互相覆盖或冲突；
3. 每次重试重新生成随机 UUID：无法命中幂等，可能重复受理；
4. 同一 `biz_id` 搭配不同手机号、正文、模板参数或定时时间：返回 409 `IDEMPOTENCY_CONFLICT`；
5. 超过 32 字符或包含不允许的空白控制字符：返回 400/422。

同键同指纹重放返回原批次且 `idempotent: true`；同键不同指纹立即 409，即使首请求仍在处理中。
首请求处理中的追随请求不消耗新发送额度，等待超时返回 503 与 `Retry-After`，调用方应继续使用原 `biz_id`。
服务端 Claim 租约固定 30 秒，由 heartbeat 同时续 PostgreSQL 与 Redis；合法受理可以超过
30/60/90/120 秒。客户端不得据此更换 `biz_id`。HTTP 超时后用原键重试：若 Claim 已
`completed` 并绑定 `batch_id`，平台返回同一批次。COMMIT 成功但响应丢失时，已绑定的
在途预留不会被 `acceptance-failed` 释放；若平台暂时无法确认提交结果，返回 503
`DEPENDENCY_UNAVAILABLE`，调用方必须用原 `biz_id` 查询或重试，禁止换键。预留已绑定
不一致批次时返回 409 `STATE_CONFLICT`，同样不得换键重发。

推荐的重试模式：

1. 每次真实业务事件生成唯一 `biz_id`；
2. 网络超时或响应丢失时，**不要更换 `biz_id` 重发**——平台可能已受理；
3. 用**相同请求体 + 相同 `biz_id`** 重试：已受理则返回原批次，未受理则正常创建新批次；
4. 不要把“查询不到旧幂等记录”解释成第一次未受理；先查 `batch_no` 或等待保护期结束。

### 3.5 定时发送

- `scheduled_at` 必须带时区（推荐 `+08:00`），如 `2026-08-05T10:00:00+08:00`；
- 可对定时批次执行取消或改期（见第 5 节）；
- 营销短信在时间窗外提交也会自动转为定时，`deferred_reason=market_window`。

## 4. 内容与模板规则

- 手机号必须为 11 位大陆手机号（`^1\d{10}$`）；
- 最终内容长度 ≤500 字符；模板参数超长会返回 `TEMPLATE_PARAM_MISMATCH`；
- 计费条在服务端计算（含签名与退订语），接入方不要自行折算配额；
- 验证码内容落库与展示会做等长打码，不影响实际下发；
- 敏感词命中（block 策略）返回 `SENSITIVE_WORD`。

### 4.1 模板发送前置条件

模板是全局资源，由平台侧集中创建和维护，不按应用部门隔离；接入方只能**使用**已审核通过的模板，不能自行创建：

1. 平台操作员/管理员创建模板（内容含 `{1}..{n}` 占位符，并为每个变量配置
   `max_len`）；
2. 模板提交厂商绑定并审核，状态变为 `approved` 后才可用于发送；
3. 平台把 `template_id`、占位符数量与各参数 `max_len` 一并交付给接入方。

请求时使用 `template_id + template_params`，**不要同时传 `content`**。
模板不存在或尚未审核通过时，发送返回 `400 INVALID_PARAM`。
接入方应**默认全部使用模板发送**；`content` 只允许作为紧急例外。

### 4.2 模板参数规则

- 参数个数必须与占位符 `{1}..{n}` 完全一致，按顺序全量替换；
- 每个参数长度不得超过对应 `max_len`（超长返回
   `422 TEMPLATE_PARAM_MISMATCH`，这是平台预防厂商侧 10002 的约定）；
- 渲染后内容 ≤500 字符；
- 计费条按渲染后的最终内容（含签名与退订语）计算，与直接内容一致。

### 4.3 审核与人工介入说明

- **API 渠道发送（含模板）不触发平台人工审批**；平台审批仅针对 Web 渠道
  且达到阈值（notice ≥100、market ≥50）的批量发送；
- 模板的"审核"发生在模板生命周期一次：`pending → 厂商审核 → approved`；
  审核通过后，后续每次模板发送均为全自动；
- **直接内容（`content`）每次提交都可能触发服务商人工审核**，发送延迟可能达数小时；
  这是正式接入必须使用已审核模板的原因；
- 敏感词、黑名单、频控等管控对模板渲染后的内容同样生效。

## 5. 批次查询与操作

接入方只能查询/操作**本应用**的批次（按 API Key 隔离）。

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/messages/batches/{batch_no}` | GET | 批次状态：`scheduled/queued/sending/completed/failed/cancelled/balance_blocked/expired`（API 渠道不产生审批状态） |
| `/api/v1/messages/batches/{batch_no}/details` | GET | 消息明细（手机号仅返回掩码），支持 `status`、`page`、`size` 参数 |
| `/api/v1/messages/batches/{batch_no}/cancel` | POST | 取消未开始发送的批次（定时/排队），非法状态返回 409 |
| `/api/v1/messages/batches/{batch_no}/reschedule` | POST | 改期，请求体 `{"scheduled_at":"2026-08-05T10:00:00+08:00"}` |
| `/api/v1/messages/batches/{batch_no}/resend-failed` | POST | 只重发失败号码，生成新批次（`resend_of` 关联原批次） |

批次列表查询目前由平台 Web 控制台提供；接入方按 `batch_no` 单查即可。

## 6. 结果回调（可选）

应用配置 `callback_url` 后，平台主动推送事件：

- `batch.finished`：批次终态汇总；
- `message.report`：消息级回执（需开启 `callback_report_enabled`）。

验签方式：

```text
X-Sms-Timestamp: <unix 秒>
X-Sms-Signature: hex(HMAC-SHA256(callback_secret, "{timestamp}.{raw_body}"))
```

- 时间戳偏差超过 300 秒应拒绝；
- 回调地址由平台侧做内网 CIDR 与 DNS 防 SSRF 校验；
- 平台对失败回调（含 4xx，不只 5xx）按 60/300/900/3600/3600 秒重试 5 次，最终置 dead 并告警；
- 接入方回调接口应返回 2xx 表示成功，且保持幂等（同一 `event_id` 可能重投）。

## 7. 限流与配额

- **应用每分钟限流**：默认 60 次请求/分钟，超限返回 `429 RATE_LIMITED`；
- **每日配额**：按计费条统计（0=不限），超限返回 `429 QUOTA_EXCEEDED`；
- 平台侧另有全局厂商 QPS 与号码级频控，接入方无需感知；
- 建议接入方客户端做本地限速与指数退避（如 1s/2s/4s，带抖动），避免触发 429。

## 8. 代码示例

### cURL

```bash
curl -X POST 'https://sms.example.com/api/v1/messages/send' \
  -H 'X-Api-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "category": "notice",
    "mobiles": ["138****8000"],
    "template_id": 12,
    "template_params": ["张三", "123456"],
    "biz_id": "ORDER-20260804-001"
  }'
```

### Python

```python
import requests

URL = "https://sms.example.com/api/v1/messages/send"

def send_sms(
    api_key: str,
    mobiles: list[str],
    template_id: int,
    template_params: list[str],
    biz_id: str,
) -> dict:
    resp = requests.post(
        URL,
        json={
            "category": "notice",
            "mobiles": mobiles,
            "template_id": template_id,
            "template_params": template_params,
            "biz_id": biz_id,
        },
        headers={"X-Api-Key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
```

### Node.js

```javascript
const response = await fetch("https://sms.example.com/api/v1/messages/send", {
  method: "POST",
  headers: {
    "X-Api-Key": process.env.SMS_API_KEY,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    category: "notice",
    mobiles: ["138****8000"],
    template_id: 12,
    template_params: ["张三", "123456"],
    biz_id: "ORDER-20260804-001",
  }),
});
const data = await response.json();
```

## 9. 错误码与排查

所有错误统一返回：

```json
{"code": "UNAUTHORIZED", "message": "API Key 无效", "detail": null}
```

| code | HTTP | 场景与排查 |
|---|---|---|
| `INVALID_PARAM` | 400 | 参数格式错误（手机号、时间、内容长度等） |
| `UNAUTHORIZED` | 401 | Key 缺失/错误/已过期/应用已停用 |
| `FORBIDDEN` | 403 | 数据权限不足 |
| `CATEGORY_NOT_ALLOWED` | 403 | 应用未开通该消息类别 |
| `IP_NOT_ALLOWED` | 403 | 来源 IP 不在应用白名单（检查出口 IP 是否变化） |
| `VENDOR_TEST_CONSOLE_ONLY` | 403 | 受控真实联调环境下的普通发送保护，仅测试环境出现 |
| `NOT_FOUND` | 404 | 批次不存在或不属于本应用 |
| `STATE_CONFLICT` | 409 | 状态机非法流转，或受理预留已绑定不一致批次 |
| `IDEMPOTENCY_CONFLICT` | 409 | 同一幂等键已用于不同请求，更换 biz_id 或复用原请求 |
| `SENSITIVE_WORD` | 422 | 内容命中敏感词 |
| `TEMPLATE_PARAM_MISMATCH` | 422 | 模板参数个数不符或超长 |
| `ALL_FILTERED` | 422 | 号码全部被去重/黑名单/频控剔除 |
| `QUOTA_EXCEEDED` | 429 | 日配额不足 |
| `RATE_LIMITED` | 429 | 请求频率超限 |
| `DEPENDENCY_UNAVAILABLE` | 503 | 依赖或 COMMIT 结果暂不可确认，用原 `biz_id` 查询后重试 |
| `USAGE_PROJECTION_UNAVAILABLE` | 503 | 配额账本暂不可用，请稍后重试（不要立即重试） |
| `BALANCE_BLOCKED` | 503 | 余额不足，平台队列暂停 |
| `VENDOR_ERROR` | 502 | 厂商侧错误（detail 仅含数值 code 与平台本地映射描述，不回传厂商原始 msg） |

## 10. 上线清单

- [ ] 应用已创建，类别/配额/限流符合业务；
- [ ] API Key 已存入服务端密钥管理，并完成轮换演练；
- [ ] 来源 IP 白名单已配置接入方稳定出口 IP；
- [ ] 模板已审核、签名已绑定；
- [ ] 确认全部发送使用已审核模板（直接内容仅限紧急例外）；
- [ ] 使用模板发送时已拿到 `template_id` 与参数规格（占位符数量、各参数 `max_len`）；
- [ ] 已实现 `biz_id` 幂等与超时重试逻辑（同键重试）；
- [ ] 已保存 `batch_no` 并实现状态查询或回调验签；
- [ ] 定时/营销场景已验证时间窗与取消流程；
- [ ] 联调环境跑通 200 受理与错误码分支（401/403/429）。

完整接口字段与示例见仓库根目录 `openapi.yaml`。
