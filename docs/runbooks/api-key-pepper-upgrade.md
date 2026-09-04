# API Key 摘要算法升级与历史 pepper 手册

独立 `api_key_pepper_key` 与 `data_hmac_key` 生命周期分离。数据库必须记录每条摘要的
算法，禁止把 `api_key_hash_version IS NULL` 静默解释为 SHA-256。

## 算法

| 算法 | 版本列 | 材料 |
|---|---|---|
| `api_pepper` | 必须绑定 pepper version | `/run/secrets/api_key_pepper_key` |
| `legacy_sha256` | 必须为 NULL | `SHA-256(api_key)` |
| `legacy_data_hmac_pepper_v1` | 必须为 NULL | 条件性 Secret `api_key_legacy_hmac_pepper` 必须保存旧 `data_hmac_key` **文件原文**；API 再按 `HMAC-SHA256("sms-api-key-pepper-v1", raw_text)` 派生。禁止把已派生的 32 字节 pepper 写入该文件 |
| `NULL` + `NULL` | 未分类 | 仅当 `sys_config.api_key_unclassified_algorithms` 给出有限候选时才验证；生产存在活动未分类行且清单为空时 readiness 失败 |

不得把当前 data HMAC JSON keyring 文本重新当作旧 pepper。

## 升级步骤

1. **盘点**：统计 `api_pepper` / `legacy_sha256` / `legacy_data_hmac_pepper_v1` / 未分类活动行。
2. **保留 legacy credential**：若存在 B 类（0085 之前用 `data_hmac_key` 原文派 pepper）活动 Key，把当时的 secret **文件原文** 放入权威源 `api_key_legacy_hmac_pepper`。`prepare_runtime_secrets.py` 会把它复制到 `current/backend/` 并只挂 API；Compose 不得把它挂给 worker。同时在 `api_key_unclassified_algorithms` 写入 `legacy_data_hmac_pepper_v1` 或 `legacy_sha256`。
3. **迁移 Schema**：应用 `0091_api_key_digest_algorithms`。已有 pepper version 的行分类为 `api_pepper`；`NULL+NULL` 保持未分类。
4. **验证**：用旧 Key 调用受保护发送接口，确认 401 不是批量出现。认证成功后允许 CAS 重哈希为当前 `api_pepper`；迁移失败不得阻断本次合法认证。
5. **逐步轮换**活动 Key；确认 current/previous 均可分别绑定不同算法。
6. **确认无引用**后清空 `api_key_unclassified_algorithms`，删除 legacy credential。
7. **回滚应用版本**时保留算法列；旧代码忽略新列，新代码不得把未分类行当 SHA-256。

## 失败关闭

- 未知算法、引用中的 pepper version 不在 keyring、需要历史 pepper 但文件缺失：readiness 失败，告警不含 Secret/摘要。
- 生产存在活动未分类行且部署清单为空：禁止接流。
- 删除仍被引用的历史 pepper 必须被预检阻止。
