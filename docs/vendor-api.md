# docs/vendor-api.md — 智慧信息-企信版 网关精确对接规格

> 依据《智慧信息-企信版 接口文档 V2.1.2》整理。`vendor/zhihui.py` 与 `vendor/mock_server.py` 的**唯一实现依据**，CLAUDE.md 附录A 仅为速查。
> 语音中心（/Voice/*）本期不对接。

## 0. 通用约定

- Base URL：`https://vendor.example.invalid`（生产）；mock：`http://mock-vendor:9028`
- 全部接口 **POST**，`Content-Type: application/json; charset=UTF-8`
- 鉴权：每个请求 Body 携带 `secretName` / `secretKey`
- **大小写警示**：官方参数表列名为 PascalCase（如 Mobile），但**请求示例均为小写驼峰**（mobile/content/...）。实现一律按示例小写驼峰；联调若报参数错误优先排查大小写
- 统一响应包络：

```json
{ "code": 0, "msg": null, "data": ... }
```

code=0 成功；非 0 见第 4 节错误码。msg 可能为 null/""/"string"。

## 1. 短信 API（8 个）

### 1.1 Send — 发送短信
`POST /Sms/Api/Send`　多号同内容群发或单发；**提交前须过滤重复号码**。

请求：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| secretName / secretKey | String | 是 | 密钥 |
| mobile | String | 是 | 逗号分隔多号码，如 `18300000000,18300000001` |
| content | String | 是 | ≤500 字 |
| templateId | String | 否 | 厂商模板ID |
| extCode | String | 否 | ≤6 位数字扩展号 |
| signName | String | 否 | 如 `【签名】`，空则用账户首个已绑签名 |
| timing | String | 否 | `yyyyMMddHHmmss`（平台不使用，自行调度） |
| customId | String | 否 | ≤36 位字母数字（平台传 32 位：批次前缀24+分片序号8） |

```json
{"secretName":"API","secretKey":"000000","mobile":"13000000000",
 "content":"你好，你的验证码是541254，有效期5分钟。",
 "templateId":"","extCode":"","signName":"","timing":"","customId":""}
```

响应：`data` = String，**短信批次唯一标识 taskId**。

```json
{ "code": 0, "msg": null, "data": "123456" }
```

### 1.2 GetReport — 获取状态报告
`POST /Sms/Api/GetReport`　仅传 secretName/secretKey。
**语义：拉走即消费**——每次返回自上次拉取以来的报告，取走后厂商侧不再重复给。实现必须先把完整原始响应字节 AES-GCM 加密落 `raw_vendor_log.payload_enc`，同时提取不含手机号的 `custom_ids` 元数据，提交后再受控解密解析；禁止把原始 JSON 明文写入 JSONB。

官方合同未提供分页、游标或单次最大条数。平台因此施加有界恢复合同：自动解析上限 4 MiB；4–64 MiB 的完整响应保全为 `complete_too_large` 仅供人工恢复；超过 64 MiB 或 spill 配额时完整性终止，截断密文标记 `truncated`。GetReport/GetReply 在发起 HTTP 之前必须先在共享 rawspill 目录原子预留 `64MiB + 文档化帧开销`（开销只由固定 64KiB 内部帧、最大帧数、控制帧和目录元数据计算，与 `aiter_raw` 网络分片无关）；预留失败则不得创建 stream、不得调用厂商，只告警等待空间恢复。网络 chunk 必须先合并为内部帧再加密落盘，禁止一对一映射为 AES-GCM 帧。`announce()` 前的 DNS/连接/TLS/超时失败必须删除纯 header-only stream 并释放该预留；启动与每轮恢复统一分类 stream/spill/tmp/marker：合法 header-only 与部分 header 超龄后删除，已连续认证 data 恢复为截断事实，未认证部分帧与损坏 `.spill`/原子写临时文件进入独立非活动 `.headerq` 隔离（自有文件数/字节配额与保留期，不占活动拉取容量），不得删除已有认证 data frame 的 stream。崩溃恢复按 source 惰性迭代、流式解码，单轮受文件数/明文字节/时间预算约束；峰值跟踪单文件而非整个 backlog，Report 不得读取 Reply payload（反之亦然）。畸形 consume-on-read `Content-Length` 仍须有界保全正文并标记 `protocol_invalid`。截断与协议异常 raw 不得当作正常可重放。升级前无法证明完整性的存量 raw 记 `unknown_legacy`，同样不得进入普通自动/运维重放。secondary `.spill` 必须把 `capture_state`/`http_status`/`content_encoding`/`source`/`payload_sha256`/`key_version` 与正文密文摘要一并纳入 AES-GCM 认证 header；恢复先验证该 header 与文件名/正文摘要一致，再允许写入 `raw_vendor_log`。未认证旧格式不得默认 `complete`，只进入 `unknown_legacy` 人工盘点。认证失败对象进入非活动隔离，不占活动拉取配额，告警不得含 PII 或密文。`.stream` 认证 terminal、`.spill` 认证 header 与库行 `capture_state` 使用同一套 replay 资格事实。Mock 与定向测试不得连接真实厂商。

`data` = Array：

| 字段 | 类型 | 说明 |
|---|---|---|
| taskId | String | 任务标识（对应 Send 返回） |
| customId | String | 我方自定义标识 |
| phone | String | 手机号 |
| reportStatus | Int | 0 未知 / 1 成功 / 2 失败 / 3 其他 / **99 余额不足** |
| reportDescription | String | 描述（可能为 CMPP 状态文案） |
| reportTime | String | ISO8601，如 `2021-05-12T06:45:17.529Z` |

```json
{"code":0,"msg":"string","data":[
 {"taskId":"string","customId":"string","phone":"string",
  "reportStatus":0,"reportDescription":"string","reportTime":"2021-05-12T06:45:17.529Z"}]}
```

⚠ 官方回复示例中出现过 `"customId "`（键名带尾随空格）的笔误，解析时对键做 `strip()` 容错。

### 1.3 GetReply — 获取上行回复
`POST /Sms/Api/GetReply`　仅密钥。同样**拉走即消费**，并适用与 GetReport 相同的 4 MiB / 64 MiB 完整性合同。大响应中的退订回复只有在完整捕获后才能自动处理；截断捕获不得进入自动重放。
`data` = Array：taskId, customId, phone, extCode, contents（回复内容）, replyTime。键名 strip 容错同上。

### 1.4 GetBalance — 查询余额
`POST /Sms/Api/GetBalance`　仅密钥。`data` = Int 余额（计费条）。

```json
{ "code": 0, "msg": "string", "data": 10000 }
```

### 1.5 BindTemplate — 申请模板
`POST /Sms/Api/BindTemplate`

| 字段 | 说明 |
|---|---|
| templateContent | **变量占位为 `{s预期变量长度}`**，例：`尊敬的{s10}您好，您的验证码是{s6}，有效时间{s1}分钟，请勿转发！` |

`data` = **Int 模板编号**（后续 Send.templateId 与状态查询使用）。

> **平台⇄厂商模板格式转换（关键）**：平台本地模板用 `{1}..{n}` 占位并为每个变量登记 max_len（sms_template.var_specs）；提交 BindTemplate 时按序转换为 `{s<max_len>}`；发送前渲染校验每个参数长度 ≤ max_len（否则厂商可能判 10002 模板不匹配）。

### 1.6 GetTemplateState — 模板状态
`POST /Sms/Api/GetTemplateState`　入参 `templateIds: Array(Int)`（支持批量，如 `[1,2,3]`）。
`data` = Array：`{id: Int, checkType: Int, checkRemark: String|null}`；checkType：**0 未审核 / 1 审核成功 / 2 审核拒绝** → 平台 pending/approved/rejected。

### 1.7 BindSign — 申请签名
`POST /Sms/Api/BindSign`　入参 `signName`（含【】，如 `"【签名】"`）。`data` = Int 签名编号。

### 1.8 GetSignState — 签名状态
`POST /Sms/Api/GetSignState`　入参 `signIds: Array(Int)`。`data` 结构同 1.6。

## 2. 信息推送（本期未启用，预留）

厂商可向我方 URL 主动推送（需线下提交客服绑定）。**字段为 PascalCase 的 JSON 数组**，与拉取接口大小写不同：

报告推送 Body：`[{"TaskId":"...","CustomId":null,"Phone":"...","ReportStatus":1,"ReportDescription":"ok","ReportTime":"2021-08-20T14:22:54.75+08:00"}]`
回复推送 Body：`[{"TaskId":"...","CustomId":null,"Phone":"...","ExtCode":"","Contents":"谢谢，收到","ReplyTime":"..."}]`
我方须应答：`{"code":0,"msg":"ok"}`。
若二期启用：接收端点建议 `/api/v1/vendor-push/report|reply`，与轮询共用幂等回写逻辑。

## 3. Mock 服务器契约（vendor/mock_server.py 必须实现）

目的：所有开发与测试零依赖真实厂商，且**可确定性注入错误**。

1. 实现第 1 节全部 8 个接口，包络/字段/大小写与本文档一致
2. 状态语义：
   - Send 返回自增 taskId；记录 customId↔taskId↔号码 映射
   - GetReport/GetReply **拉走即消费**：返回未消费项并标记已消费
   - 报告默认在 Send 后 2s 生成 reportStatus=1
3. 魔法号码（确定性行为）：
   - `1990000****` → 报告 reportStatus=2（失败）
   - `1991000****` → 永不生成报告（测 48h unknown）
4. 控制端点 `POST /_mock/state`（测试专用）：
   ```json
   {"next_send_code":5002,"times":1}     // 下1次Send返回该错误码
   {"clear_send_error":true}               // 清除尚未消费的Send错误注入
   {"latency_ms":12000}                  // 注入延迟(触发客户端10s超时→uncertain)
   {"balance":5000}                      // 设置GetBalance返回值
   {"requeue_reports":true}             // 重新入队已消费报告(测幂等)
   {"enqueue_report":{"taskId":"legacy-1","customId":"legacy-x","phone":"13800000000","reportStatus":1}} // 注入无主报告
   {"enqueue_reply":{"taskId":"1","customId":"x","phone":"13800000000","contents":"TD"}}              // 注入上行回复
   {"callback_failures":5,"callback_status":500} // mock callback sink 前5次返回500，之后200
   {"clear_callbacks":true}             // 清空已接收callback记录
   {"retain_callback_count":3}          // 仅保留callback前3项，用于测试后精确恢复
   {"reset":true}
   ```
5. `GET /_mock/state` 返回当前配置（含 callback_failures/status）、未消费队列长度、Send 调用记录（含实际下发内容，仅测试进程内存保存）、BindTemplate 内容列表和 callback 接收计数（断言用）
6. dev-only callback sink：`POST /_mock/callback` 接收平台 webhook，保存 raw body 与签名头到内存；按 callback_failures/status 确定性失败。`GET /_mock/callbacks` 返回已接收事件，`DELETE /_mock/callbacks` 清空。测试应用 callback_url 固定为 `http://mock-vendor:9028/_mock/callback`
7. `enqueue_report/enqueue_reply` 仅接受 `^1\d{10}$` 测试号；状态端点响应手机号始终 mask，只有 mock 进程内 Send 断言接口可按测试权限核对 OTP 原文

## 4. 错误代码表（完整，vendor/codes.py 依据）

| code | 描述 | 平台处理 |
|---|---|---|
| 0 | 成功 | — |
| 9 | 失败 | chunk failed，不重试 |
| 429 | 请求过多 | 指数退避重试 |
| 999 | 余额不足 | balance_blocked + 告警 + 双队列暂停（注：**99 仅出现在 reportStatus**，错误码是 999） |
| 1000 | 账号/密码错误 | crit 告警，暂停队列 |
| 1001 | 手机号码错误 | chunk failed，不重试 |
| 1002 | 内容格式错误 | chunk failed，不重试 |
| 1003 | 模板id错误 | chunk failed，同步模板状态 |
| 1004 | 定时时间格式错误 | 不适用（平台不用厂商定时） |
| 1005 | 自定义id不可超过36位 | 编码缺陷，crit 告警 |
| 1006 | 号码数已达上限 | 折半 vendor_batch_size 重试一次 |
| 1007 | 定时必须大于10分钟 | 不适用 |
| 1008 | 未支持的套餐 | crit 告警（商务侧） |
| 1009 | 账户未启用 | crit 告警，暂停队列 |
| 1010 | IP校验未通过 | crit 告警（出口 IP 需主备双报备） |
| 1011 | 未在服务时间范围 | 延迟重试(30min) |
| 1012 | 内容字数已达上限 | chunk failed，不重试 |
| 5000 | 缺省参数未配置 | crit 告警，暂停双队列（配置错误） |
| 5001 | 其他错误 | chunk failed，记录待排查 |
| 5002 | 调用频率过快(间隔) | 指数退避重试(≤5次) |
| 5003 | 调用频次过快(每秒) | 指数退避重试(≤5次) |
| 10000 | 扩展号错误 | chunk failed（平台不传 extCode，出现即缺陷） |
| 10001 | 短信签名错误 | chunk failed，同步签名状态 |
| 10002 | 短信模板不匹配 | chunk failed，提示核对模板/参数长度 |
| 10003 | 短信密钥账号不存在 | crit 告警，暂停队列 |
| 10004 | 未激活短信业务 | crit 告警，暂停双队列 |
| 10005 | 内容存在全局关键字 | chunk failed，提示修改内容 |
| 10006 | Excel短信格式错误 | 不适用（平台不用厂商Excel通道） |
| 10007 | 短信内容中存在签名 | chunk failed（平台应提前校验避免） |
| 10008 | 未开通短信模板 | crit 告警（商务侧） |
| 10009 | 连接方式错误 | crit 告警 |
| 10010 | 当前操作已被锁定 | 延迟重试(5min) |
| 10011 | 模板数量已达上限 | 申请接口返回用户 |
| 10012 | 存在相同的模板 | 申请接口返回用户 |
| 10013 | 存在相同的签名 | 申请接口返回用户 |
| 10014 | 签名数量已达上限 | 申请接口返回用户 |
| (HTTP超时/网络异常) | 结果未知 | **chunk=uncertain，禁自动重发，reconcile 比对修复** |

CMPP-SUBMIT_RESP 状态码（0成功/9失败/14余额不足/16手机号错误…）可能出现在 reportDescription 文案中，仅用于展示，不参与状态机。
