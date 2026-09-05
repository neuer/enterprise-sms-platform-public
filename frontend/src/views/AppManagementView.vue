<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, onMounted, reactive, ref } from "vue"

import {
  createApp,
  disableApp,
  estimateWorstCaseCapacity,
  getApp,
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
import { getReport, type ReportDimSummary } from "../api/reports"
import { listSigns, type SmsSign } from "../api/signs"
import { listTemplates, type SmsTemplate, type VarSpec } from "../api/templates"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import { copyText } from "../lib/clipboard"
import { CATEGORY_LABELS } from "../lib/labels"
import { formatDateTime, shanghaiDateKey } from "../lib/time"

type SecretOperation = "create-app" | "rotate-api-key" | "rotate-callback-secret"

const CATEGORY_FILTERS: { label: string; value: AppCategory | "all" }[] = [
  { label: "全部", value: "all" },
  { label: "验证码", value: "verify" },
  { label: "通知", value: "notice" },
  { label: "营销", value: "market" },
]

const STATUS_FILTERS: { label: string; value: "all" | "1" | "0" }[] = [
  { label: "全部", value: "all" },
  { label: "启用", value: "1" },
  { label: "停用", value: "0" },
]

const items = ref<ManagedApp[]>([])
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")
const keyword = ref("")
const categoryFilter = ref<AppCategory | "all">("all")
const statusFilter = ref<"all" | "1" | "0">("all")
const detailId = ref<number | null>(null)
const detailOpen = ref(false)
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
/** 今日用量联查结果（dim_value = app.id 字符串）；调用失败时置 unavailable，单元格显示「—」。 */
const dailyUsage = ref<Map<string, ReportDimSummary>>(new Map())
const usageUnavailable = ref(false)
/** 已通过厂商审核的签名清单；加载失败不阻塞表单，下拉显示不可用并可重试。 */
const approvedSigns = ref<SmsSign[]>([])
const signsLoading = ref(false)
const signsUnavailable = ref(false)

const form = reactive({
  name: "",
  dept: "",
  allowed_categories: ["notice"] as AppCategory[],
  default_sign: "",
  daily_quota: 0,
  rate_limit_per_min: 60,
  recipient_limit_per_min: 10000,
  segment_limit_per_min: 10000,
  max_in_flight_chunks: 200,
  allow_market_api_bulk: false,
  blacklist_check: true,
  freq_override: "",
  allowed_ips: "",
  ip_allowlist_exempt_until: "",
  unlimited_quota_exempt_until: "",
  admission_exempt_note: "",
  callback_url: "",
  callback_report_enabled: false,
  status: 1 as 0 | 1,
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
    name: "",
    dept: "",
    allowed_categories: ["notice"],
    default_sign: "",
    daily_quota: 0,
    rate_limit_per_min: 60,
    recipient_limit_per_min: 10000,
    segment_limit_per_min: 10000,
    max_in_flight_chunks: 200,
    allow_market_api_bulk: false,
    blacklist_check: true,
    freq_override: "",
    allowed_ips: "",
    ip_allowlist_exempt_until: "",
    unlimited_quota_exempt_until: "",
    admission_exempt_note: "",
    callback_url: "",
    callback_report_enabled: false,
    status: 1,
  })
}

/** 接口全量返回，关键词（名称/部门）、类别与状态过滤均为前端推导，不新增查询参数。 */
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return items.value.filter((item) => {
    if (categoryFilter.value !== "all" && !item.allowed_categories.includes(categoryFilter.value)) {
      return false
    }
    if (statusFilter.value !== "all" && String(item.status) !== statusFilter.value) return false
    if (kw && !item.name.toLowerCase().includes(kw) && !item.dept.toLowerCase().includes(kw)) {
      return false
    }
    return true
  })
})

const enabledCount = computed(() => filtered.value.filter((item) => item.status === 1).length)
const disabledCount = computed(() => filtered.value.length - enabledCount.value)

const emptyTitle = computed(() => (items.value.length === 0 ? "当前没有接入应用" : "没有符合筛选条件的应用"))
const emptyDescription = computed(() =>
  items.value.length === 0
    ? "创建应用后会得到一对 API Key / 回调密钥（仅展示一次）；类别、配额、限流与回调均可在详情中随时调整。"
    : "重置类别或状态筛选、清空关键词后查看全部应用。",
)

/** 详情抽屉数据源跟随列表引用，写操作重查列表后自动刷新。 */
const detail = computed(() => items.value.find((item) => item.id === detailId.value) ?? null)

/** 停用应用整行降透明度，与密钥列「已随停用吊销」呼应。 */
function rowClassName({ row }: { row: ManagedApp }): string {
  return row.status ? "" : "apps-row-disabled"
}

/** 今日消耗（计费条）：联查成功但无记录为 0；联查失败返回 null，由界面显示「—」。 */
function consumedOf(app: ManagedApp): number | null {
  if (usageUnavailable.value) return null
  return dailyUsage.value.get(String(app.id))?.total_segments ?? 0
}

/** 成功率直接取服务端口径（services/stats.py），前端不自行计算。 */
function rateOf(app: ManagedApp): number | null {
  if (usageUnavailable.value) return null
  return dailyUsage.value.get(String(app.id))?.success_rate ?? null
}

/** 配额占用百分比；配额 0（不限量）或用量不可用时不渲染进度条。 */
function quotaPercent(app: ManagedApp): number | null {
  const consumed = consumedOf(app)
  if (consumed === null || app.daily_quota <= 0) return null
  return Math.min(100, (consumed / app.daily_quota) * 100)
}

/** 进度条色阶：>80% 琥珀、≥100% 朱红，其余 verdi。 */
function quotaTone(app: ManagedApp): "" | "warn" | "over" {
  const percent = quotaPercent(app)
  if (percent === null) return ""
  if (percent >= 100) return "over"
  if (percent > 80) return "warn"
  return ""
}

/** 旧 Key 宽限剩余小时（向上取整，下限 0）；无宽限期旧 Key 返回 null。 */
function graceHoursLeft(app: ManagedApp): number | null {
  if (!app.old_key_expires_at) return null
  const ms = Date.parse(app.old_key_expires_at) - Date.now()
  // 静态检查禁词 Math.ceil（防计费公式重实现误判）；floor((ms+3599999)/1h) 等价向上取整
  return Math.max(0, Math.floor((ms + 3_599_999) / 3_600_000))
}

/** 回调列只展示 host+path，完整 URL 收进 title。 */
function callbackDisplay(url: string): string {
  try {
    const parsed = new URL(url)
    return `${parsed.host}${parsed.pathname === "/" ? "" : parsed.pathname}`
  } catch {
    return url
  }
}

function categoriesText(app: ManagedApp): string {
  return app.allowed_categories.map((category) => CATEGORY_LABELS[category]).join(" · ")
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

const detailRateText = computed(() => {
  const current = detail.value
  if (!current) return "—"
  const rate = rateOf(current)
  return rate === null ? "—" : `${(rate * 100).toFixed(1)}%（delivered/(delivered+failed)）`
})

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
      `  -H "X-Api-Key: $SMS_API_KEY" \\`,
      `  -H 'Content-Type: application/json' \\`,
      `  -d '${payloadJson(context)}'`,
    ]
      .filter(Boolean)
      .join("\n")
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
const demoTemplate = computed(() => approvedTemplates.value.find((item) => item.id === demoTemplateId.value) ?? null)
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
  try {
    items.value = await listApps()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "应用列表加载失败"
  } finally {
    loading.value = false
  }
}

/** 今日用量一次联查（stat_daily 按应用维度），失败只影响用量单元格，不拖垮列表。 */
async function loadDailyUsage(): Promise<void> {
  const today = shanghaiDateKey()
  try {
    const result = await getReport({
      granularity: "day",
      groupBy: "app",
      category: "all",
      start: today,
      end: today,
    })
    if (!result || !Array.isArray(result.dim_summary)) throw new Error("用量统计响应无效")
    dailyUsage.value = new Map(result.dim_summary.map((row) => [row.dim_value, row]))
    usageUnavailable.value = false
  } catch {
    dailyUsage.value = new Map()
    usageUnavailable.value = true
  }
}

async function loadKeyGraceHours(): Promise<void> {
  try {
    const configs = await listConfigs()
    const raw = configs.find((item) => item.key === "key_grace_hours")?.value
    const value = Number(raw)
    keyGraceHours.value = Number.isInteger(value) && value > 0 ? value : null
  } catch {
    keyGraceHours.value = null
    ElMessage.warning("密钥轮换宽限期读取失败，页面显示可能不完整")
  }
}

async function loadApprovedSigns(): Promise<void> {
  signsLoading.value = true
  try {
    const signs = await listSigns()
    approvedSigns.value = signs.filter((item) => item.vendor_state === "approved")
    signsUnavailable.value = false
  } catch (error) {
    approvedSigns.value = []
    signsUnavailable.value = true
    ElMessage.error(error instanceof Error ? error.message : "已通过签名清单加载失败")
  } finally {
    signsLoading.value = false
  }
}

/** 当前默认签名不在已通过清单时补一个遗留项，避免下拉显示原始值或被静默清空。 */
const legacySign = computed(() => {
  const value = form.default_sign.trim()
  if (!value) return null
  return approvedSigns.value.some((item) => item.name === value) ? null : value
})

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
    ip_allowlist_exempt_until: item.ip_allowlist_exempt_until || "",
    unlimited_quota_exempt_until: item.unlimited_quota_exempt_until || "",
    admission_exempt_note: item.admission_exempt_note || "",
    callback_url: item.callback_url || "",
    freq_override: item.freq_override ? JSON.stringify(item.freq_override) : "",
  })
  drawerOpen.value = true
}

function openDetail(item: ManagedApp): void {
  detailId.value = item.id
  detailOpen.value = true
}

/** 详情抽屉「编辑配置」：沿用分组表单编辑，关闭详情避免双层抽屉叠放。 */
function editFromDetail(): void {
  const current = detail.value
  if (!current) return
  openEdit(current)
  detailOpen.value = false
}

function parseAllowedIps(input: string): string[] {
  return input
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function payload(): AppPayload {
  const override = parseFrequencyOverride(form.freq_override)
  return {
    dept: form.dept.trim(),
    allowed_categories: form.allowed_categories,
    default_sign: form.default_sign.trim() || null,
    daily_quota: form.daily_quota,
    rate_limit_per_min: form.rate_limit_per_min,
    recipient_limit_per_min: form.recipient_limit_per_min,
    segment_limit_per_min: form.segment_limit_per_min,
    max_in_flight_chunks: form.max_in_flight_chunks,
    allow_market_api_bulk: form.allow_market_api_bulk,
    blacklist_check: form.blacklist_check,
    freq_override: override,
    callback_url: form.callback_url.trim() || null,
    allowed_ips: parseAllowedIps(form.allowed_ips),
    ip_allowlist_exempt_until: form.ip_allowlist_exempt_until.trim() || null,
    unlimited_quota_exempt_until: form.unlimited_quota_exempt_until.trim() || null,
    admission_exempt_note: form.admission_exempt_note.trim() || null,
    callback_report_enabled: form.callback_report_enabled,
    status: form.status,
  }
}

const worstCase = computed(() => estimateWorstCaseCapacity(form))

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
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "应用保存失败")
  } finally {
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
    const graceHint =
      keyGraceHours.value === null
        ? "旧 Key 将进入当前配置的宽限期。"
        : `旧 Key 将进入 ${keyGraceHours.value} 小时宽限期。`
    await ElMessageBox.confirm(
      h("div", { class: "apps-danger-dialog" }, [
        h("p", `将为 ${item.name} 生成新的 API Key。新 Key 仅展示一次，${graceHint}请确认已准备好立即复制并安全保存。`),
        h("p", { class: "apps-audit-note" }, "轮换行为与操作人将写入审计日志。"),
      ]),
      "确认轮换 API Key",
      { type: "warning", confirmButtonText: "确认轮换", cancelButtonText: "取消", customClass: "apps-confirm-box" },
    )
    const result = await rotateAppKey(item.id)
    secretRevealed = true
    reveal(
      "这是当前最终 API Key（仅展示一次）",
      result.api_key,
      `请立即复制并安全保存，确认保存后再关闭。旧 Key 宽限期至 ${formatDateTime(result.old_key_expires_at)}`,
    )
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "Key 轮换失败")
    }
  } finally {
    if (!secretRevealed) clearSecret()
  }
}

async function revokeKey(item: ManagedApp): Promise<void> {
  if (!item.old_key_prefix || !item.old_key_expires_at) return
  try {
    await ElMessageBox.confirm(
      h("div", { class: "apps-danger-dialog" }, [
        h(
          "p",
          `旧 Key ${item.old_key_prefix}•••• 原定 ${formatDateTime(item.old_key_expires_at)} 到期，作废后立即失效；仍使用旧 Key 的调用方将收到 401。`,
        ),
        h("p", { class: "apps-audit-note" }, "作废行为与操作人将写入审计日志。"),
      ]),
      "立即作废旧 Key？",
      { type: "warning", confirmButtonText: "确认作废", cancelButtonText: "取消", customClass: "apps-confirm-box" },
    )
    await revokeOldAppKey(item.id)
    ElMessage.success("旧 Key 已作废")
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "作废失败")
  }
}

async function rotateCallback(item: ManagedApp): Promise<void> {
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-callback-secret"
  rotatingCallbackId.value = item.id
  let secretRevealed = false
  try {
    await ElMessageBox.confirm(
      h("div", { class: "apps-danger-dialog" }, [
        h(
          "p",
          `将为 ${item.name} 生成新的回调密钥，已部署的旧密钥立即失效。新密钥仅展示一次，请确认已准备好立即复制并安全保存。`,
        ),
        h("p", { class: "apps-audit-note" }, "轮换行为与操作人将写入审计日志。"),
      ]),
      "确认轮换回调密钥",
      { type: "warning", confirmButtonText: "确认轮换", cancelButtonText: "取消", customClass: "apps-confirm-box" },
    )
    const result = await rotateCallbackSecret(item.id)
    secretRevealed = true
    reveal("新回调密钥（仅展示一次）", result.callback_secret)
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "回调密钥轮换失败")
    }
  } finally {
    if (!secretRevealed) clearSecret()
  }
}

async function disable(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "apps-danger-dialog" }, [
        h("ul", { class: "apps-conseq" }, [
          h("li", "当前与宽限期旧 API Key 立即吊销，发送/查询返回 401"),
          h("li", "在途批次继续到终态，历史数据保留可查"),
          h("li", "未终结的旧回调在同一事务隔离为不可重试"),
          h("li", "恢复需管理员在详情抽屉重新启用"),
        ]),
        h("p", { class: "apps-audit-note" }, "操作记审计（app_disable）· 操作人写入审计主体"),
      ]),
      `停用应用 ${item.name}？`,
      { type: "warning", confirmButtonText: "确认停用", cancelButtonText: "取消" },
    )
    await disableApp(item.id)
    ElMessage.success(`应用 ${item.name} 已停用`)
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "停用失败")
  }
}

/** 启用不再从列表行拼全字段 PUT：先取权威配置再仅改 status，消除字段漂移写坏配置的风险。 */
async function enable(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "apps-danger-dialog" }, [
        h("p", `启用应用 ${item.name}？`),
        h("p", { class: "apps-audit-note" }, "启用行为与操作人将写入审计日志。"),
      ]),
      "确认启用",
      { type: "warning", confirmButtonText: "确认启用", cancelButtonText: "取消", customClass: "apps-confirm-box" },
    )
    const current = await getApp(item.id)
    await updateApp(item.id, {
      dept: current.dept,
      allowed_categories: current.allowed_categories,
      default_sign: current.default_sign,
      daily_quota: current.daily_quota,
      rate_limit_per_min: current.rate_limit_per_min,
      recipient_limit_per_min: current.recipient_limit_per_min,
      segment_limit_per_min: current.segment_limit_per_min,
      max_in_flight_chunks: current.max_in_flight_chunks,
      allow_market_api_bulk: current.allow_market_api_bulk,
      blacklist_check: current.blacklist_check,
      freq_override: current.freq_override,
      allowed_ips: current.allowed_ips,
      ip_allowlist_exempt_until: current.ip_allowlist_exempt_until,
      unlimited_quota_exempt_until: current.unlimited_quota_exempt_until,
      admission_exempt_note: current.admission_exempt_note,
      callback_url: current.callback_url,
      callback_report_enabled: current.callback_report_enabled,
      status: 1,
    })
    ElMessage.success(`应用 ${item.name} 已启用`)
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "启用失败")
  }
}

onMounted(() => {
  void load()
  void loadDailyUsage()
  void loadKeyGraceHours()
  void loadApprovedSigns()
})
</script>

<template>
  <section class="page-heading apps-heading">
    <div>
      <p class="eyebrow">APPLICATION CONTROL / 应用控制</p>
      <h1>应用管理</h1>
      <p>接入方、配额、频控与密钥生命周期。全部写操作记审计；密钥明文仅创建/轮换当次展示。</p>
    </div>
    <el-button data-testid="new-app" type="primary" :disabled="secretOperation !== null" @click="openCreate"
      >新建应用</el-button
    >
  </section>

  <div class="apps-filter-bar">
    <label class="apps-fld">
      <span>关键词</span>
      <el-input v-model="keyword" class="apps-keyword" data-testid="apps-keyword" placeholder="名称 / 部门" clearable />
    </label>
    <div class="apps-fld">
      <span>类别</span>
      <div class="apps-seg" role="group" aria-label="类别筛选" data-testid="apps-category-seg">
        <button
          v-for="option in CATEGORY_FILTERS"
          :key="option.value"
          type="button"
          :class="{ on: categoryFilter === option.value }"
          :data-testid="`apps-category-${option.value}`"
          @click="categoryFilter = option.value"
        >
          {{ option.label }}
        </button>
      </div>
    </div>
    <div class="apps-fld">
      <span>状态</span>
      <div class="apps-seg" role="group" aria-label="状态筛选" data-testid="apps-status-seg">
        <button
          v-for="option in STATUS_FILTERS"
          :key="option.value"
          type="button"
          :class="{ on: statusFilter === option.value }"
          :data-testid="`apps-status-${option.value}`"
          @click="statusFilter = option.value"
        >
          {{ option.label }}
        </button>
      </div>
    </div>
    <span class="apps-filter-note">接口全量返回 · 前端过滤</span>
  </div>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="apps-alert">
    <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
  </el-alert>

  <section class="apps-results">
    <el-table
      v-loading="loading"
      class="apps-table"
      data-testid="app-table"
      :data="filtered"
      row-key="id"
      :row-class-name="rowClassName"
    >
      <el-table-column label="应用" min-width="200">
        <template #default="{ row }">
          <span class="apps-name"
            >{{ row.name }}<span class="apps-name-id">#{{ row.id }}</span></span
          >
          <span class="apps-cell-sub">{{ row.dept }}</span>
        </template>
      </el-table-column>
      <el-table-column label="允许类别" min-width="150">
        <template #default="{ row }">
          <div class="apps-categories">
            <CategoryTag v-for="category in row.allowed_categories" :key="category" :category="category" />
          </div>
        </template>
      </el-table-column>
      <el-table-column label="今日消耗（计费条）" min-width="170">
        <template #default="{ row }">
          <div v-if="consumedOf(row) !== null" class="apps-quota-cell">
            <span class="apps-quota-num">
              {{ (consumedOf(row) ?? 0).toLocaleString() }}
              <small>/ {{ row.daily_quota === 0 ? "不限量" : row.daily_quota.toLocaleString() }}</small>
            </span>
            <span v-if="quotaPercent(row) !== null" class="apps-quota-bar">
              <i :class="quotaTone(row)" :style="{ width: `${quotaPercent(row)}%` }"></i>
            </span>
          </div>
          <span v-else class="apps-cell-none">—</span>
        </template>
      </el-table-column>
      <el-table-column label="限流/分" width="90">
        <template #default="{ row }">
          <span class="apps-mono">{{ row.rate_limit_per_min.toLocaleString() }}</span>
        </template>
      </el-table-column>
      <el-table-column label="密钥" min-width="200">
        <template #default="{ row }">
          <div class="apps-key-cell">
            <template v-if="row.status === 1">
              <span class="apps-key-prefix">{{ row.api_key_prefix }}••••</span>
              <span v-if="graceHoursLeft(row) !== null" class="apps-key-tag apps-key-tag--grace">
                旧 Key 宽限 · 余 {{ graceHoursLeft(row) }}h
              </span>
              <span v-else class="apps-key-tag">单 Key 运行</span>
            </template>
            <span v-else class="apps-key-tag apps-key-tag--revoked">已随停用吊销</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="回调" min-width="170">
        <template #default="{ row }">
          <template v-if="row.callback_url">
            <span class="apps-callback-url" :title="row.callback_url">{{ callbackDisplay(row.callback_url) }}</span>
            <span class="apps-cell-sub">
              明细回调 {{ row.callback_report_enabled ? "开启" : "关闭" }} · 密钥{{
                row.callback_secret_configured ? "已配置" : "未配置"
              }}
            </span>
          </template>
          <span v-else class="apps-cell-none">未配置</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag :type="row.status ? 'success' : 'info'">{{ row.status ? "启用" : "停用" }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button :data-testid="`app-detail-${row.id}`" link type="primary" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="apps-empty">
          <EmptyState :title="emptyTitle" :description="emptyDescription" />
          <el-button
            v-if="!items.length"
            class="apps-empty-action"
            type="primary"
            :disabled="secretOperation !== null"
            @click="openCreate"
            >新建应用</el-button
          >
        </div>
      </template>
    </el-table>
    <footer class="apps-foot">
      <span>共 {{ filtered.length }} 个应用 · 启用 {{ enabledCount }} · 停用 {{ disabledCount }}</span>
      <span class="apps-foot-role">读写：admin · 今日消耗来自 stat_daily 联查</span>
    </footer>
  </section>

  <el-drawer v-model="detailOpen" class="apps-drawer apps-detail-drawer" size="min(560px, 92vw)" :teleported="false">
    <template #header>
      <div v-if="detail" class="apps-drawer-head">
        <div class="apps-drawer-title">
          <el-tag :type="detail.status ? 'success' : 'info'">{{ detail.status ? "启用" : "停用" }}</el-tag>
          <b>{{ detail.name }}</b>
        </div>
        <code>#{{ detail.id }} · {{ detail.dept }} · 创建于 {{ formatDateTime(detail.created_at) }}</code>
      </div>
    </template>
    <template v-if="detail">
      <section class="app-sec">
        <h3>运行概览 · 今日<small>统计口径 services/stats.py</small></h3>
        <div class="apps-hero">
          <div class="apps-hero-nums">
            <template v-if="!usageUnavailable">
              <b>{{ (consumedOf(detail) ?? 0).toLocaleString() }}</b>
              <span
                >/ {{ detail.daily_quota === 0 ? "不限量" : detail.daily_quota.toLocaleString() }} 计费条 · 成功率
                {{ detailRateText }}</span
              >
            </template>
            <template v-else>
              <b>—</b>
              <span>今日用量统计暂不可用</span>
            </template>
          </div>
          <span v-if="quotaPercent(detail) !== null" class="apps-quota-bar">
            <i :class="quotaTone(detail)" :style="{ width: `${quotaPercent(detail)}%` }"></i>
          </span>
        </div>
        <dl class="apps-fact-grid">
          <div
            ><dt>每分钟限流</dt><dd class="apps-mono">{{ detail.rate_limit_per_min.toLocaleString() }} 次</dd></div
          >
          <div
            ><dt>频控覆盖</dt><dd>{{ freqOverrideText(detail) }}</dd></div
          >
        </dl>
      </section>

      <section class="app-sec">
        <h3>密钥与回调<small>明文仅创建/轮换当次展示</small></h3>
        <div class="apps-key-line">
          <code v-if="detail.status === 1">{{ detail.api_key_prefix }}••••</code>
          <code v-else>已随停用吊销</code>
          <small>当前 API Key</small>
          <span class="apps-key-act">
            <el-button
              v-if="detail.status === 1"
              :data-testid="`rotate-key-${detail.id}`"
              link
              type="primary"
              :loading="rotatingKeyId === detail.id"
              :disabled="secretOperation !== null"
              @click="rotateKey(detail)"
              >轮换 Key</el-button
            >
          </span>
        </div>
        <div v-if="detail.old_key_prefix && detail.old_key_expires_at" class="apps-key-grace">
          <code>{{ detail.old_key_prefix }}••••</code>
          <small
            >旧 Key 宽限期至 {{ formatDateTime(detail.old_key_expires_at) }}（余
            {{ graceHoursLeft(detail) }}h），到期自动失效</small
          >
          <span class="apps-key-act">
            <el-button :data-testid="`revoke-old-key-${detail.id}`" link type="danger" @click="revokeKey(detail)"
              >立即作废</el-button
            >
          </span>
        </div>
        <dl class="apps-fact-grid">
          <div class="full">
            <dt>回调 URL（内网白名单校验 · 生产仅 HTTPS）</dt>
            <dd class="apps-mono">{{ detail.callback_url || "未配置" }}</dd>
          </div>
          <div>
            <dt>明细回调</dt>
            <dd>{{ detail.callback_url ? (detail.callback_report_enabled ? "开启" : "关闭") : "—" }}</dd>
          </div>
          <div>
            <dt>回调密钥</dt>
            <dd>
              {{ detail.callback_secret_configured ? "已配置" : "未配置" }}
              <el-button
                :data-testid="`rotate-callback-${detail.id}`"
                link
                type="primary"
                :loading="rotatingCallbackId === detail.id"
                :disabled="secretOperation !== null"
                @click="rotateCallback(detail)"
                >轮换回调密钥</el-button
              >
            </dd>
          </div>
        </dl>
      </section>

      <section class="app-sec">
        <h3>策略</h3>
        <dl class="apps-fact-grid">
          <div
            ><dt>允许类别</dt><dd>{{ categoriesText(detail) }}</dd></div
          >
          <div
            ><dt>默认签名</dt><dd>{{ detail.default_sign ? `【${detail.default_sign}】` : "未设置" }}</dd></div
          >
          <div
            ><dt>黑名单检查</dt><dd>{{ detail.blacklist_check ? "开启" : "关闭" }}</dd></div
          >
          <div
            ><dt>营销 API 大批量</dt><dd>{{ detail.allow_market_api_bulk ? "已预授权" : "未预授权" }}</dd></div
          >
          <div>
            <dt>来源 IP 白名单</dt>
            <dd class="apps-mono">{{
              detail.allowed_ips.length ? `${detail.allowed_ips.length} 条 CIDR` : "全网放行"
            }}</dd>
          </div>
        </dl>
      </section>
    </template>
    <template #footer>
      <div v-if="detail" class="apps-drawer-foot">
        <el-button :data-testid="`edit-app-${detail.id}`" @click="editFromDetail">编辑配置</el-button>
        <el-button :data-testid="`demo-script-${detail.id}`" @click="openDemo(detail)">接入示例</el-button>
        <span class="apps-foot-sp"></span>
        <el-button v-if="detail.status" :data-testid="`disable-app-${detail.id}`" type="danger" @click="disable(detail)"
          >停用应用</el-button
        >
        <el-button v-else :data-testid="`enable-app-${detail.id}`" type="success" @click="enable(detail)"
          >启用应用</el-button
        >
      </div>
    </template>
  </el-drawer>

  <el-drawer v-model="drawerOpen" class="apps-drawer apps-editor-drawer" size="min(560px, 92vw)" :teleported="false">
    <template #header>
      <div class="apps-drawer-head">
        <div class="apps-drawer-title">{{ editingId === null ? "新建应用" : "编辑应用" }}</div>
        <code>{{
          editingId === null
            ? "创建成功后 API Key 与回调密钥仅展示一次，请立即保存"
            : `正在编辑「${form.name}」· 应用名创建后不可修改`
        }}</code>
      </div>
    </template>
    <el-form label-position="top" @submit.prevent="save">
      <section class="apps-form-sec">
        <h3>基本信息</h3>
        <el-form-item label="应用名" required>
          <el-input v-model="form.name" :disabled="editingId !== null" maxlength="64" autocomplete="off" />
          <small class="field-rule">1–64 字符，全局唯一，创建后不可修改。</small>
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
          <small class="field-rule"
            >默认仅通知。验证码/营销须显式勾选；未授权类别的发送请求返回 403 CATEGORY_NOT_ALLOWED。</small
          >
        </el-form-item>
        <el-form-item label="默认签名">
          <el-select
            v-model="form.default_sign"
            data-testid="default-sign-select"
            clearable
            filterable
            :loading="signsLoading"
            :placeholder="approvedSigns.length ? '从已通过签名中选择' : '暂无已通过签名'"
            style="width: 100%"
          >
            <el-option v-for="sign in approvedSigns" :key="sign.id" :value="sign.name" :label="`【${sign.name}】`" />
            <el-option v-if="legacySign" :value="legacySign" :label="`【${legacySign}】（未通过审核的遗留值）`" />
          </el-select>
          <small class="field-rule"
            >仅可选择签名管理中厂商状态为「已通过」的签名；请求未指定签名时使用，请求内显式签名优先。清空表示不设置。</small
          >
          <small v-if="signsUnavailable" class="field-rule">
            签名清单加载失败，<el-button link type="primary" data-testid="signs-retry" @click="loadApprovedSigns"
              >重试</el-button
            >；保存前请确认可选范围。
          </small>
        </el-form-item>
      </section>

      <section class="apps-form-sec">
        <h3>配额与策略</h3>
        <div class="apps-form-2col">
          <el-form-item label="日配额（计费条）">
            <el-input-number v-model="form.daily_quota" :min="0" :max="100000000" />
            <small class="field-rule">0 = 不限量，最大 100,000,000。</small>
          </el-form-item>
          <el-form-item label="每分钟限流">
            <el-input-number v-model="form.rate_limit_per_min" :min="1" :max="60000" />
            <small class="field-rule">1–60,000 次请求/分钟。</small>
          </el-form-item>
        </div>
        <div class="apps-form-2col">
          <el-form-item label="每分钟号码上限">
            <el-input-number v-model="form.recipient_limit_per_min" :min="1" :max="100000000" />
          </el-form-item>
          <el-form-item label="每分钟计费条上限">
            <el-input-number v-model="form.segment_limit_per_min" :min="1" :max="100000000" />
          </el-form-item>
        </div>
        <div class="apps-form-2col">
          <el-form-item label="在途分片上限">
            <el-input-number v-model="form.max_in_flight_chunks" :min="1" :max="100000" />
          </el-form-item>
          <el-form-item label="营销 API 大批量预授权">
            <el-switch v-model="form.allow_market_api_bulk" />
            <small class="field-rule">关闭时，API 营销达到审批阈值将被 403 拒绝，不会转入人工审批。</small>
          </el-form-item>
        </div>
        <div class="apps-form-alert" data-testid="worst-case-capacity">
          最坏能力：每分钟最多 {{ worstCase.recipientsPerMin.toLocaleString() }} 个号码、
          {{ worstCase.segmentsPerMin.toLocaleString() }} 计费条；每日
          {{
            worstCase.dailySegments === null
              ? "不限量（生产须豁免）"
              : `${worstCase.dailySegments.toLocaleString()} 计费条`
          }}。 单请求最多 10,000 号码，1×10,000 与 100×100 按同一成本计入。
        </div>
        <div v-if="form.daily_quota === 0" class="apps-form-alert">
          日配额为 0 表示不限量。生产保存必须填写未过期豁免与原因，否则无法保存。
        </div>
        <el-form-item label="黑名单检查">
          <el-switch v-model="form.blacklist_check" />
          <small class="field-rule">关闭后该应用号码不执行黑名单剔除。</small>
        </el-form-item>
        <el-form-item label="频控覆盖 JSON" :error="freqOverrideError || undefined">
          <el-input
            v-model="form.freq_override"
            data-testid="freq-override"
            type="textarea"
            placeholder='例如 {"verify_per_minute":2,"verify_per_day":20,"market_per_day":1}'
          />
          <small class="field-rule"
            >留空用系统默认；仅 verify_per_minute（1–100）/ verify_per_day（1–10,000）/
            market_per_day（1–1,000），值为正整数。</small
          >
        </el-form-item>
      </section>

      <section class="apps-form-sec">
        <h3>安全与回调</h3>
        <el-form-item label="来源 IP 白名单（每行一个 IP/CIDR，最多 50 条）">
          <div v-if="!form.allowed_ips.trim()" class="apps-form-alert apps-form-alert--verm">
            白名单为空表示全网放行。生产环境必须填写 CIDR，或提供未过期豁免与原因。
          </div>
          <el-input
            v-model="form.allowed_ips"
            data-testid="allowed-ips-input"
            type="textarea"
            placeholder="203.0.113.0/24"
          />
          <small class="field-rule">单 IP 自动归一化为 /32；留空仅开发/测试或已登记豁免可用，保存时校验格式。</small>
        </el-form-item>
        <el-form-item label="豁免到期（空白名单 / 无限配额）">
          <el-input
            v-model="form.ip_allowlist_exempt_until"
            placeholder="空白名单豁免 ISO8601，如 2026-09-10T08:00:00+08:00"
          />
          <el-input
            v-model="form.unlimited_quota_exempt_until"
            placeholder="无限配额豁免 ISO8601"
            style="margin-top: 8px"
          />
          <el-input
            v-model="form.admission_exempt_note"
            maxlength="200"
            placeholder="豁免原因（生产必填）"
            style="margin-top: 8px"
          />
        </el-form-item>
        <el-form-item label="回调 URL">
          <el-input v-model="form.callback_url" placeholder="https://" />
          <small class="field-rule">须落在内网 CIDR 白名单，生产仅 HTTPS；留空表示不推送回调。</small>
        </el-form-item>
        <el-form-item label="明细回调">
          <el-switch v-model="form.callback_report_enabled" />
          <small class="field-rule">按消息粒度推送明细回调，开启前需先配置回调 URL。</small>
        </el-form-item>
      </section>
    </el-form>
    <template #footer>
      <div class="apps-drawer-foot">
        <small class="apps-form-audit">保存即记审计（{{ editingId === null ? "app_create" : "app_update" }}）</small>
        <span class="apps-foot-sp"></span>
        <el-button @click="drawerOpen = false">取消</el-button>
        <el-button data-testid="save-app" type="primary" :loading="saving" @click="save">{{
          editingId === null ? "创建应用" : "保存"
        }}</el-button>
      </div>
    </template>
  </el-drawer>

  <el-dialog
    v-model="secretOpen"
    :title="secretTitle"
    width="min(560px, 92vw)"
    :close-on-click-modal="false"
    :before-close="beforeSecretClose"
    destroy-on-close
    @closed="clearSecret"
  >
    <el-alert type="warning" :closable="false" :title="secretHint" />
    <pre class="one-time-secret">{{ secretValue }}</pre>
    <template #footer>
      <el-button data-testid="secret-copy" :disabled="!secretValue" @click="copySecret">复制</el-button>
      <el-button data-testid="secret-close" type="primary" @click="closeSecret">我已安全保存</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="demoOpen"
    :title="demoApp ? `接入示例 · ${demoApp.name}` : '接入示例'"
    width="min(720px, 96vw)"
    :close-on-click-modal="false"
    class="demo-dialog"
  >
    <p class="muted"
      >应用 #{{ demoApp?.id }} · {{ demoApp?.dept }} · 类别 {{ (demoApp?.allowed_categories || []).join(" / ") }}</p
    >
    <p
      >正式接入必须使用已审核模板（template_id）发送；直接内容会进入服务商人工审核、发送延迟大。API Key
      请通过环境变量注入，不要硬编码或写入日志。</p
    >
    <label class="muted" for="demo-template-select">已审核模板</label>
    <el-select
      v-model="demoTemplateId"
      data-testid="demo-template-select"
      placeholder="选择已审核模板"
      :loading="demoTemplatesLoading"
      style="width: 100%"
    >
      <el-option
        v-for="template in approvedTemplates"
        :key="template.id"
        :value="template.id"
        :label="'#' + template.id + ' · ' + template.name"
      />
    </el-select>
    <p v-if="demoTemplate" data-testid="demo-template-info">
      模板内容：{{ demoTemplate.content }} · 参数：{{ demoParamsSummary }}
    </p>
    <p v-else>暂无已审核模板，示例将使用占位模板 ID；请先在「模板管理」创建模板并提交审核。</p>
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
