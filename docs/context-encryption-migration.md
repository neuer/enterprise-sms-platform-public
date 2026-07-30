# 上下文绑定密文迁移

本流程把历史通用 AAD/SMSX1 密文迁移到 v2 上下文 envelope/SMSX2。迁移全程保留旧
AES/HMAC key version；任何中间明文只允许存在于单条记录或单个导出帧的进程内存，不得
写入临时文件、日志、审计、队列或错误详情。

## 范围与上下文

| 数据 | domain | table.column | object ID |
|---|---|---|---|
| 消息手机号 | `phone` | `sms_message.phone_enc` | 同行 `phone_hmac` |
| 导入/回复/未匹配报告/测试号码 | `phone` | 各事实表 `.phone_enc` | 同行 `phone_hmac` |
| 实际下发正文 | `sms-content` | `sms_batch.send_content_enc` | `batch_no` |
| 回调签名密钥 | `callback-secret` | `app.callback_secret_enc` | 不可变应用名 |
| 厂商原始响应 | `vendor-raw` | `raw_vendor_log.payload_enc` | `source:payload_sha256` |
| 导出帧 | `export-frame` | `export_task.file_ciphertext` | `task_id:lease_id:frame_index:kind` |

## 分阶段执行

1. 先部署双读、新写 v2 的版本；保留当前 keyring 的全部历史版本，并记录各表旧格式数量。
2. 在隔离恢复库抽样验证每类旧密文可读、上下文字段完整且 HMAC/摘要一致。任何不一致
   立即停止，禁止猜测 object ID、跳过认证或填充占位值。
3. 由独立 owner 维护进程按主键小批领取记录，事务内 `FOR UPDATE SKIP LOCKED`；解密一
   条后立即用明确上下文和活动 key version 重加密，CAS 条件同时匹配主键、旧密文和旧
   key version。每批提交后清空内存，仅记录表名、计数和安全任务 ID。
4. 导出文件不原地改写：旧 SMSX1 只读，下载完成后由新的 export task 生成 SMSX2；
   到期 housekeeping 删除旧文件。禁止把解密 CSV 落盘再重新导入。
5. 重复执行统计和抽样验证，直到所有数据库旧格式与有效期内 SMSX1 文件计数为零；保持
   至少一个发布观察窗并验证 callback 重试仍使用任务创建时的 secret/signature version。
6. 只有完成加密备份与隔离恢复验证后，才可在后续版本把业务读路径改为
   `allow_legacy=False`；确认没有记录引用旧 key version 后再按密钥变更单移除旧密钥。

## 回退

迁移写入的 v2 密文仍由双读版本直接读取，因此回退只允许回到支持 v2 的前一发布版本。
不得回退到只认识旧 AAD/SMSX1 的代码。单批失败时事务回滚，保留原密文；修复上下文或
密钥问题后从该批次继续，禁止恢复为明文或整体覆盖历史数据。
