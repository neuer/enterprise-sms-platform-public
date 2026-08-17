<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, reactive, ref } from "vue"

import {
  createApp,
  disableApp,
  listApps,
  parseFrequencyOverride,
  revokeOldAppKey,
  rotateAppKey,
  rotateCallbackSecret,
  updateApp,
  type AppCategory,
  type AppPayload,
  type ManagedApp,
} from "../api/apps"
import { listConfigs } from "../api/admin"
import { listTemplates, type SmsTemplate, type VarSpec } from "../api/templates"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import { copyText } from "../lib/clipboard"

type SecretOperation = "create-app" | "rotate-api-key" | "rotate-callback-secret"

const items = ref<ManagedApp[]>([])
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")
const drawerOpen = ref(false)
const editingId = ref<number | null>(null)
const secretOpen = ref(false)
const secretTitle = ref("")
const secretValue = ref("")
const secretHint = ref("")
const keyGraceHours = ref<number | null>(null)
const secretOperation = ref<SecretOperation | null>(null)
const rotatingKeyId = ref<number | null>(null)
const rotatingCallbackId = ref<number | null>(null)

const form = reactive({
  name: "", dept: "", allowed_categories: ["notice"] as AppCategory[], default_sign: "",
  daily_quota: 0, rate_limit_per_min: 60, blacklist_check: true, freq_override: "",
  allowed_ips: "", callback_url: "", callback_report_enabled: false, status: 1 as 0 | 1,
})

/** 频控覆盖输入失焦前的内联校验；保存时仍由 payload() 兜底。 */
const freqOverrideError = computed(() => {
  if (!form.freq_override.trim()) return ""
  try {
    parseFrequencyOverride(form.freq_override)
    return ""
  } catch (error) {
    return error instanceof Error ? error.message : "频控覆盖 JSON 无效"
  }
})

function resetForm(): void {
  Object.assign(form, {
    name: "", dept: "", allowed_categories: ["notice"], default_sign: "",
    daily_quota: 0, rate_limit_per_min: 60, blacklist_check: true, freq_override: "",
    allowed_ips: "", callback_url: "", callback_report_enabled: false, status: 1,
  })
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
    .format(new Date(value))
    .replaceAll("/", "-")
}

function freqOverrideText(item: ManagedApp): string {
  const override = item.freq_override
  if (!override) return "未覆盖"
  const parts: string[] = []
  if (override.verify_per_minute) parts.push(`验证码 ${override.verify_per_minute}/分`)
  if (override.verify_per_day) parts.push(`验证码 ${override.verify_per_day}/日`)
  if (override.market_per_day) parts.push(`营销 ${override.market_per_day}/日`)
  return parts.join(" · ") || "未覆盖"
}

function messageCallbackText(item: ManagedApp): string {
  if (!item.callback_url) return "—"
  return item.callback_report_enabled ? "开启" : "关闭"
}

const DEMO_LANGUAGES = ["curl", "python", "node", "java", "go", "php"] as const
type DemoLanguage = (typeof DEMO_LANGUAGES)[number]

const DEMO_LABELS: Record<DemoLanguage, string> = {
  curl: "cURL",
  python: "Python",
  node: "Node.js",
  java: "Java",
  go: "Go",
  php: "PHP",
}

interface DemoContext {
  app: ManagedApp
  templateId: number
  templateName: string
  templateContent: string
  params: string[]
}

const DEMO_BIZ_ID = "ORDER-20260804-001"
const DEMO_MOBILES = ["138****8000"]

/** 按 var_specs 生成不超过 max_len 的示例参数值。 */
function exampleParamsFor(specs: VarSpec[]): string[] {
  const sorted = [...specs].sort((a, b) => a.pos - b.pos)
  return sorted.map((spec) => {
    for (const candidate of [`示例参数${spec.pos}`, `参数${spec.pos}`, "示例", "值"]) {
      if (candidate.length <= spec.max_len) return candidate
    }
    return "a".repeat(spec.max_len)
  })
}

/** 生成目标语言可用的模板参数数组字面量。 */
function paramsLiteral(language: DemoLanguage, params: string[]): string {
  const quoted = params.map((value) => `"${value}"`)
  if (language === "java") return `new String[]{${quoted.join(", ")}}`
  if (language === "go") return `[]string{${quoted.join(", ")}}`
  if (language === "php") return `[${params.map((value) => `'${value}'`).join(", ")}]`
  return `[${quoted.join(", ")}]`
}

/** 生成与语言无关的请求 JSON（cURL 与 Java 文本块直接内嵌）。 */
function payloadJson(context: DemoContext): string {
  return JSON.stringify({
    category: "notice",
    mobiles: DEMO_MOBILES,
    template_id: context.templateId,
    template_params: context.params,
    biz_id: DEMO_BIZ_ID,
  })
}

/** 生成指定语言的模板发送 demo；API Key 一律使用环境变量占位，不嵌入明文。 */
function buildDemoScript(language: DemoLanguage, context: DemoContext): string {
  const base = "https://sms.example.com/api/v1"
  const head = `// 应用：${context.app.name}（id=${context.app.id}）· 模板：${context.templateName}（id=${context.templateId}）`
  const warning = "// 正式接入必须使用已审核模板，直接内容会进入服务商人工审核"
  const baseHint = "// 请把 base 替换为平台地址（测试环境 http://<服务器IP>:18080/api/v1）"
  const contentLine = context.templateContent ? `// 模板内容：${context.templateContent}` : ""
  const params = paramsLiteral(language, context.params)
  if (language === "curl") {
    return [
      `# 应用：${context.app.name}（id=${context.app.id}）· 模板：${context.templateName}（id=${context.templateId}）`,
      contentLine,
      `# 正式接入必须使用已审核模板，直接内容会进入服务商人工审核`,
      `# 请把 base 替换为平台地址（测试环境 http://<服务器IP>:18080/api/v1）`,
      `curl -X POST '${base}/messages/send' \\`,
      `  -H 'X-Api-Key: $SMS_API_KEY' \\`,
      `  -H 'Content-Type: application/json' \\`,
      `  -d '${payloadJson(context)}'`,
    ].filter(Boolean).join("\n")
  }
  const common: string[] = [head, warning, baseHint]
  if (contentLine) common.splice(1, 0, contentLine)
  if (language === "python") {
    return [
      ...common,
      `import os`,
      `import requests`,
      ``,
      `URL = "${base}/messages/send"`,
      ``,
      `def send_template(mobiles, template_id, template_params, biz_id):`,
      `    resp = requests.post(`,
      `        URL,`,
      `        json={`,
      `            "category": "notice",`,
      `            "mobiles": mobiles,`,
      `            "template_id": template_id,`,
      `            "template_params": template_params,`,
      `            "biz_id": biz_id,`,
      `        },`,
      `        headers={"X-Api-Key": os.environ["SMS_API_KEY"]},`,
      `        timeout=15,`,
      `    )`,
      `    resp.raise_for_status()`,
      `    return resp.json()`,
      ``,
      `print(send_template(${JSON.stringify(DEMO_MOBILES)}, ${context.templateId}, ${params}, "${DEMO_BIZ_ID}"))`,
    ].join("\n")
  }
  if (language === "node") {
    return [
      ...common,
      `const SMS_API_KEY = process.env.SMS_API_KEY;`,
      `const TEMPLATE_ID = ${context.templateId};`,
      `const URL = "${base}/messages/send";`,
      ``,
      `const response = await fetch(URL, {`,
      `  method: "POST",`,
      `  headers: {`,
      `    "X-Api-Key": SMS_API_KEY,`,
      `    "Content-Type": "application/json",`,
      `  },`,
      `  body: JSON.stringify({`,
      `    category: "notice",`,
      `    mobiles: ${JSON.stringify(DEMO_MOBILES)},`,
      `    template_id: TEMPLATE_ID,`,
      `    template_params: ${params},`,
      `    biz_id: "${DEMO_BIZ_ID}",`,
      `  }),`,
      `});`,
      `const data = await response.json();`,
      `console.log(data.batch_no, data.status, data.quota_cost);`,
    ].join("\n")
  }
  if (language === "java") {
    return [
      ...common,
      `import java.net.URI;`,
      `import java.net.http.HttpClient;`,
      `import java.net.http.HttpRequest;`,
      `import java.net.http.HttpResponse;`,
      ``,
      `public class SmsDemo {`,
      `    public static void main(String[] args) throws Exception {`,
      `        String body = """`,
      `            ${payloadJson(context)}`,
      `            """;`,
      `        HttpRequest request = HttpRequest.newBuilder()`,
      `            .uri(URI.create("${base}/messages/send"))`,
      `            .header("X-Api-Key", System.getenv("SMS_API_KEY"))`,
      `            .header("Content-Type", "application/json")`,
      `            .POST(HttpRequest.BodyPublishers.ofString(body))`,
      `            .build();`,
      `        HttpResponse<String> response = HttpClient.newHttpClient()`,
      `            .send(request, HttpResponse.BodyHandlers.ofString());`,
      `        System.out.println(response.statusCode());`,
      `        System.out.println(response.body());`,
      `    }`,
      `}`,
    ].join("\n")
  }
  if (language === "go") {
    return [
      ...common,
      `package main`,
      ``,
      `import (`,
      `    "bytes"`,
      `    "encoding/json"`,
      `    "fmt"`,
      `    "io"`,
      `    "net/http"`,
      `    "os"`,
      `)`,
      ``,
      `func main() {`,
      `    payload, _ := json.Marshal(map[string]interface{}{`,
      `        "category":        "notice",`,
      `        "mobiles":         []string{"138****8000"},`,
      `        "template_id":     ${context.templateId},`,
      `        "template_params": ${params},`,
      `        "biz_id":          "${DEMO_BIZ_ID}",`,
      `    })`,
      `    req, _ := http.NewRequest("POST", "${base}/messages/send", bytes.NewReader(payload))`,
      `    req.Header.Set("X-Api-Key", os.Getenv("SMS_API_KEY"))`,
      `    req.Header.Set("Content-Type", "application/json")`,
      `    resp, _ := http.DefaultClient.Do(req)`,
      `    defer resp.Body.Close()`,
      `    body, _ := io.ReadAll(resp.Body)`,
      `    fmt.Println(resp.StatusCode, string(body))`,
      `}`,
    ].join("\n")
  }
  return [
    ...common,
    `<?php`,
    `$url = '${base}/messages/send';`,
    `$payload = json_encode([`,
    `    'category' => 'notice',`,
    `    'mobiles' => ['138****8000'],`,
    `    'template_id' => ${context.templateId},`,
    `    'template_params' => ${params},`,
    `    'biz_id' => '${DEMO_BIZ_ID}',`,
    `]);`,
    `$ch = curl_init($url);`,
    `curl_setopt_array($ch, [`,
    `    CURLOPT_POST => true,`,
    `    CURLOPT_POSTFIELDS => $payload,`,
    `    CURLOPT_HTTPHEADER => [`,
    `        'X-Api-Key: ' . getenv('SMS_API_KEY'),`,
    `        'Content-Type: application/json',`,
    `    ],`,
    `    CURLOPT_RETURNTRANSFER => true,`,
    `    CURLOPT_TIMEOUT => 15,`,
    `]);`,
    `$response = curl_exec($ch);`,
    `$status = curl_getinfo($ch, CURLINFO_HTTP_CODE);`,
    `curl_close($ch);`,
    `echo $status, "\\n", $response, "\\n";`,
  ].join("\n")
}

const demoOpen = ref(false)
const demoApp = ref<ManagedApp | null>(null)
const demoLang = ref<DemoLanguage>("curl")
const approvedTemplates = ref<SmsTemplate[]>([])
const demoTemplatesLoading = ref(false)
const demoTemplateId = ref<number | null>(null)
const demoTemplate = computed(() =>
  approvedTemplates.value.find((item) => item.id === demoTemplateId.value) ?? null,
)
const demoParamsSummary = computed(() => {
  const template = demoTemplate.value
  if (!template || !template.var_specs.length) return "无变量"
  return [...template.var_specs]
    .sort((a, b) => a.pos - b.pos)
    .map((spec) => `{${spec.pos}}≤${spec.max_len}`)
    .join("，")
})
const demoContext = computed<DemoContext | null>(() => {
  const app = demoApp.value
  const template = demoTemplate.value
  if (!app || !template) return null
  return {
    app,
    templateId: template.id,
    templateName: template.name,
    templateContent: template.content,
    params: exampleParamsFor(template.var_specs),
  }
})
const demoScript = computed(() => {
  const app = demoApp.value
  if (!app) return ""
  const context = demoContext.value ?? {
    app,
    templateId: 12,
    templateName: "（请选择已审核模板）",
    templateContent: "",
    params: ["张三", "123456"],
  }
  return buildDemoScript(demoLang.value, context)
})

async function loadApprovedTemplates(): Promise<void> {
  demoTemplatesLoading.value = true
  try {
    const templates = await listTemplates()
    approvedTemplates.value = templates.filter((item) => item.vendor_state === "approved")
    if (demoTemplateId.value === null && approvedTemplates.value.length) {
      demoTemplateId.value = approvedTemplates.value[0].id
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "模板加载失败")
    approvedTemplates.value = []
  } finally {
    demoTemplatesLoading.value = false
  }
}

function openDemo(item: ManagedApp): void {
  demoApp.value = item
  demoLang.value = "curl"
  demoTemplateId.value = null
  demoOpen.value = true
  void loadApprovedTemplates()
}

async function copyDemo(): Promise<void> {
  if (!demoScript.value) return
  if (await copyText(demoScript.value)) {
    ElMessage.success("脚本已复制到剪贴板")
  } else {
    ElMessage.error("复制失败，请手动选择文本复制")
  }
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try { items.value = await listApps() }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : "应用列表加载失败" }
  finally { loading.value = false }
}

async function loadKeyGraceHours(): Promise<void> {
  try {
    const configs = await listConfigs()
    const raw = configs.find((item) => item.key === "key_grace_hours")?.value
    const value = Number(raw)
    keyGraceHours.value = Number.isInteger(value) && value > 0 ? value : null
  } catch {
    keyGraceHours.value = null
  }
}

function openCreate(): void {
  editingId.value = null
  resetForm()
  drawerOpen.value = true
}

function openEdit(item: ManagedApp): void {
  editingId.value = item.id
  Object.assign(form, {
    ...item,
    default_sign: item.default_sign || "",
    allowed_ips: item.allowed_ips.join("\n"),
    callback_url: item.callback_url || "",
    freq_override: item.freq_override ? JSON.stringify(item.freq_override) : "",
  })
  drawerOpen.value = true
}

function parseAllowedIps(input: string): string[] {
  return input.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
}

function payload(): AppPayload {
  const override = parseFrequencyOverride(form.freq_override)
  return {
    dept: form.dept.trim(), allowed_categories: form.allowed_categories,
    default_sign: form.default_sign.trim() || null, daily_quota: form.daily_quota,
    rate_limit_per_min: form.rate_limit_per_min, blacklist_check: form.blacklist_check,
    freq_override: override, callback_url: form.callback_url.trim() || null,
    allowed_ips: parseAllowedIps(form.allowed_ips),
    callback_report_enabled: form.callback_report_enabled, status: form.status,
  }
}

function reveal(title: string, value: string, hint = "请立即保存；关闭后平台不会再次展示。") {
  secretTitle.value = title
  secretValue.value = value
  secretHint.value = hint
  secretOpen.value = true
}

function clearSecret(): void {
  secretTitle.value = ""
  secretValue.value = ""
  secretHint.value = ""
  secretOperation.value = null
  rotatingKeyId.value = null
  rotatingCallbackId.value = null
}

function closeSecret(): void {
  clearSecret()
  secretOpen.value = false
}

function beforeSecretClose(done: () => void): void {
  clearSecret()
  done()
}

async function copySecret(): Promise<void> {
  if (!secretValue.value) return
  if (await copyText(secretValue.value)) {
    ElMessage.success("已复制到剪贴板")
  } else {
    ElMessage.error("复制失败，请手动选择文本复制")
  }
}

async function save(): Promise<void> {
  if (!form.name.trim() || !form.dept.trim() || !form.allowed_categories.length) {
    ElMessage.warning("请填写应用名、部门并选择至少一个类别")
    return
  }
  const targetId = editingId.value
  const creating = targetId === null
  if (creating && secretOperation.value !== null) return
  if (creating) secretOperation.value = "create-app"
  let secretRevealed = false
  saving.value = true
  try {
    const body = payload()
    if (creating) {
      const name = form.name.trim()
      const result = await createApp({ ...body, name })
      const credentials = result.callback_secret
        ? `API Key: ${result.api_key}\nCallback Secret: ${result.callback_secret}`
        : result.api_key
      secretRevealed = true
      reveal(
        "应用凭据（仅展示一次）",
        credentials,
        `应用「${name}」的凭据仅展示一次，请立即复制并安全保存；关闭后平台不会再次展示。`,
      )
    } else {
      await updateApp(targetId, body)
      ElMessage.success("应用配置已更新")
    }
    drawerOpen.value = false
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "应用保存失败") }
  finally {
    saving.value = false
    if (creating && !secretRevealed) clearSecret()
  }
}

async function rotateKey(item: ManagedApp): Promise<void> {
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-api-key"
  rotatingKeyId.value = item.id
  let secretRevealed = false
  try {
    const graceHint = keyGraceHours.value === null
      ? "旧 Key 将进入当前配置的宽限期。"
      : `旧 Key 将进入 ${keyGraceHours.value} 小时宽限期。`
    await ElMessageBox.confirm(
      `将为 ${item.name} 生成新的 API Key。新 Key 仅展示一次，${graceHint}请确认已准备好立即复制并安全保存。`,
      "确认轮换 API Key",
      { type: "warning", confirmButtonText: "确认轮换", cancelButtonText: "取消" },
    )
    const result = await rotateAppKey(item.id)
    secretRevealed = true
    reveal(
      "这是当前最终 API Key（仅展示一次）",
      result.api_key,
      `请立即复制并安全保存，确认保存后再关闭。旧 Key 宽限期至 ${formatTime(result.old_key_expires_at)}`,
    )
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "Key 轮换失败")
    }
  } finally { if (!secretRevealed) clearSecret() }
}

async function revokeKey(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(`立即作废 ${item.name} 的旧 Key？`, "确认作废", { type: "warning" })
    await revokeOldAppKey(item.id)
    ElMessage.success("旧 Key 已作废")
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "作废失败") }
}

async function rotateCallback(item: ManagedApp): Promise<void> {
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-callback-secret"
  rotatingCallbackId.value = item.id
  let secretRevealed = false
  try {
    await ElMessageBox.confirm(
      `将为 ${item.name} 生成新的回调密钥，已部署的旧密钥立即失效。新密钥仅展示一次，请确认已准备好立即复制并安全保存。`,
      "确认轮换回调密钥",
      { type: "warning", confirmButtonText: "确认轮换", cancelButtonText: "取消" },
    )
    const result = await rotateCallbackSecret(item.id)
    secretRevealed = true
    reveal("新回调密钥（仅展示一次）", result.callback_secret)
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "回调密钥轮换失败")
    }
  } finally { if (!secretRevealed) clearSecret() }
}

async function disable(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(`停用应用 ${item.name}？`, "确认停用", { type: "warning" })
    await disableApp(item.id)
    ElMessage.success(`应用 ${item.name} 已停用`)
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "停用失败") }
}

async function enable(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(`启用应用 ${item.name}？`, "确认启用", { type: "warning" })
    await updateApp(item.id, {
      dept: item.dept,
      allowed_categories: item.allowed_categories,
      default_sign: item.default_sign,
      daily_quota: item.daily_quota,
      rate_limit_per_min: item.rate_limit_per_min,
      blacklist_check: item.blacklist_check,
      freq_override: item.freq_override,
      allowed_ips: item.allowed_ips,
      callback_url: item.callback_url,
      callback_report_enabled: item.callback_report_enabled,
      status: 1,
    })
    ElMessage.success(`应用 ${item.name} 已启用`)
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "启用失败") }
}

onMounted(() => {
  void load()
  void loadKeyGraceHours()
})
</script>

<template>
  <section class="page-heading">
    <div>
      <p class="eyebrow">APPLICATION CONTROL / 应用控制</p>
      <h1>应用管理</h1>
      <p>管理调用方、配额、频控与密钥生命周期。</p>
    </div>
    <el-button data-testid="new-app" type="primary" :disabled="secretOperation !== null" @click="openCreate">新建应用</el-button>
  </section>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="apps-error">
    <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
  </el-alert>

  <section v-loading="loading" class="app-card-grid" aria-label="应用与密钥">
    <article v-for="row in items" :key="row.id" :class="['managed-app-card', { disabled: !row.status }]">
      <header>
        <div>
          <strong>{{ row.name }}</strong>
          <small>#{{ row.id }} · {{ row.dept }}</small>
        </div>
        <el-tag :type="row.status ? 'success' : 'info'">{{ row.status ? '启用' : '停用' }}</el-tag>
      </header>
      <div class="app-management-categories">
        <CategoryTag v-for="category in row.allowed_categories" :key="category" :category="category" />
      </div>
      <dl>
        <div><dt>日配额</dt><dd>{{ row.daily_quota === 0 ? '不限量' : row.daily_quota.toLocaleString() }}</dd></div>
        <div><dt>每分钟限流</dt><dd>{{ row.rate_limit_per_min.toLocaleString() }}</dd></div>
        <div><dt>默认签名</dt><dd>{{ row.default_sign || '未设置' }}</dd></div>
      </dl>
      <dl class="managed-app-policy">
        <div><dt>黑名单检查</dt><dd>{{ row.blacklist_check ? '开启' : '关闭' }}</dd></div>
        <div><dt>频控覆盖</dt><dd :title="row.freq_override ? freqOverrideText(row) : undefined">{{ freqOverrideText(row) }}</dd></div>
        <div><dt>来源白名单</dt><dd>{{ row.allowed_ips.length ? `${row.allowed_ips.length} 条` : '不限' }}</dd></div>
        <div><dt>回调地址</dt><dd :title="row.callback_url || undefined">{{ row.callback_url || '未配置' }}</dd></div>
        <div><dt>消息级回调</dt><dd>{{ messageCallbackText(row) }}</dd></div>
      </dl>
      <div class="managed-key">
        <span>API KEY</span>
        <code>sk-••••••••••</code>
        <small>{{ keyGraceHours === null ? '旧 Key 宽限期暂不可用' : `旧 Key 宽限期 ${keyGraceHours} 小时` }}</small>
      </div>
      <footer>
        <el-button :data-testid="`demo-script-${row.id}`" link @click="openDemo(row)">接入示例</el-button>
        <el-button :data-testid="`edit-app-${row.id}`" link type="primary" @click="openEdit(row)">编辑</el-button>
        <el-button :data-testid="`rotate-key-${row.id}`" link type="primary" :loading="rotatingKeyId === row.id" :disabled="secretOperation !== null" @click="rotateKey(row)">轮换 Key</el-button>
        <el-button :data-testid="`revoke-key-${row.id}`" link @click="revokeKey(row)">作废旧 Key</el-button>
        <el-button :data-testid="`rotate-callback-${row.id}`" link :loading="rotatingCallbackId === row.id" :disabled="secretOperation !== null" @click="rotateCallback(row)">轮换回调密钥</el-button>
        <el-button v-if="row.status" :data-testid="`disable-app-${row.id}`" link type="danger" @click="disable(row)">停用</el-button>
        <el-button v-else :data-testid="`enable-app-${row.id}`" link type="success" @click="enable(row)">启用</el-button>
      </footer>
    </article>
    <EmptyState v-if="!loading && !items.length" title="当前没有接入应用" description="创建应用后可配置类别、配额、限流和回调。" />
  </section>

  <el-drawer v-model="drawerOpen" :title="editingId === null ? '新建应用' : '编辑应用'" size="min(560px, 100vw)" :teleported="false" class="apps-drawer">
    <p class="drawer-intro">{{ editingId === null ? '创建成功后 API Key 与回调密钥仅展示一次，请立即保存。' : `正在编辑应用「${form.name}」，应用名创建后不可修改。` }}</p>
    <el-form label-position="top" @submit.prevent="save">
      <el-form-item label="应用名" required>
        <el-input v-model="form.name" :disabled="editingId !== null" maxlength="64" autocomplete="off" />
        <small class="field-rule">1–64 字符，创建后不可修改。</small>
      </el-form-item>
      <el-form-item label="部门" required>
        <el-input v-model="form.dept" maxlength="128" />
        <small class="field-rule">1–128 字符，用于部门级日配额归集。</small>
      </el-form-item>
      <el-form-item label="允许类别" required>
        <el-checkbox-group v-model="form.allowed_categories">
          <el-checkbox value="verify">验证码</el-checkbox>
          <el-checkbox value="notice">通知</el-checkbox>
          <el-checkbox value="market">营销</el-checkbox>
        </el-checkbox-group>
        <small class="field-rule">至少选择一个类别；未授权类别的发送请求会被拒绝。</small>
      </el-form-item>
      <el-form-item label="默认签名">
        <el-input v-model="form.default_sign" />
        <small class="field-rule">发送请求未指定签名时使用；请求内显式签名优先。</small>
      </el-form-item>
      <el-form-item label="日配额">
        <el-input-number v-model="form.daily_quota" :min="0" :max="100000000" />
        <small class="field-rule">每日计费条上限，0 表示不限量，最大 100,000,000。</small>
      </el-form-item>
      <el-form-item label="每分钟限流">
        <el-input-number v-model="form.rate_limit_per_min" :min="1" :max="60000" />
        <small class="field-rule">1–60,000 条/分钟。</small>
      </el-form-item>
      <el-form-item label="黑名单检查">
        <el-switch v-model="form.blacklist_check" />
        <small class="field-rule">关闭后该应用的号码不再执行黑名单剔除。</small>
      </el-form-item>
      <el-form-item label="频控覆盖 JSON" :error="freqOverrideError || undefined">
        <el-input v-model="form.freq_override" data-testid="freq-override" type="textarea" placeholder='例如 {"verify_per_minute":2,"verify_per_day":20,"market_per_day":1}' />
        <small class="field-rule">留空使用系统默认频控；仅支持 verify_per_minute（1–100）、verify_per_day（1–10,000）、market_per_day（1–1,000），值为正整数。</small>
      </el-form-item>
      <el-form-item label="来源 IP 白名单（每行一个 IP/CIDR，空=不限）">
        <el-input v-model="form.allowed_ips" data-testid="allowed-ips-input" type="textarea" placeholder="203.0.113.0/24" />
        <small class="field-rule">仅作用于 X-Api-Key 路径；留空表示不限制来源，保存时校验格式。</small>
      </el-form-item>
      <el-form-item label="回调 URL">
        <el-input v-model="form.callback_url" placeholder="https://" />
        <small class="field-rule">必须落在系统允许的内网 CIDR 白名单内，保存时校验；留空表示不推送回调。</small>
      </el-form-item>
      <el-form-item label="消息级回调">
        <el-switch v-model="form.callback_report_enabled" />
        <small class="field-rule">按消息粒度推送回执，需先配置回调 URL。</small>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="drawerOpen=false">取消</el-button>
      <el-button data-testid="save-app" type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-drawer>

  <el-dialog v-model="secretOpen" :title="secretTitle" width="min(560px, 92vw)" :close-on-click-modal="false" :before-close="beforeSecretClose" destroy-on-close @closed="clearSecret">
    <el-alert type="warning" :closable="false" :title="secretHint" />
    <pre class="one-time-secret">{{ secretValue }}</pre>
    <template #footer>
      <el-button data-testid="secret-copy" :disabled="!secretValue" @click="copySecret">复制</el-button>
      <el-button data-testid="secret-close" type="primary" @click="closeSecret">我已安全保存</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="demoOpen" :title="demoApp ? `接入示例 · ${demoApp.name}` : '接入示例'" width="min(720px, 96vw)" :close-on-click-modal="false" class="demo-dialog">
    <p class="muted">应用 #{{ demoApp?.id }} · {{ demoApp?.dept }} · 类别 {{ (demoApp?.allowed_categories || []).join(' / ') }}</p>
    <p class="hint">正式接入必须使用已审核模板（template_id）发送；直接内容会进入服务商人工审核、发送延迟大。API Key 请通过环境变量注入，不要硬编码或写入日志。</p>
    <label class="muted" for="demo-template-select">已审核模板</label>
    <el-select v-model="demoTemplateId" data-testid="demo-template-select" placeholder="选择已审核模板" :loading="demoTemplatesLoading" style="width: 100%">
      <el-option v-for="template in approvedTemplates" :key="template.id" :value="template.id" :label="'#' + template.id + ' · ' + template.name" />
    </el-select>
    <p v-if="demoTemplate" class="hint" data-testid="demo-template-info">
      模板内容：{{ demoTemplate.content }} · 参数：{{ demoParamsSummary }}
    </p>
    <p v-else class="hint">暂无已审核模板，示例将使用占位模板 ID；请先在「模板管理」创建模板并提交审核。</p>
    <el-tabs v-model="demoLang">
      <el-tab-pane v-for="language in DEMO_LANGUAGES" :key="language" :label="DEMO_LABELS[language]" :name="language">
        <pre class="demo-script" :data-testid="`demo-script-body-${language}`">{{ demoScript }}</pre>
      </el-tab-pane>
    </el-tabs>
    <template #footer>
      <el-button data-testid="demo-copy" :disabled="!demoScript" @click="copyDemo">复制脚本</el-button>
      <el-button data-testid="demo-close" type="primary" @click="demoOpen = false">关闭</el-button>
    </template>
  </el-dialog>
</template>
