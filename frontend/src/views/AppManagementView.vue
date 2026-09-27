<script setup lang="ts">
import { CATEGORY_OPTIONS } from "../lib/labels"
import ApiDemoDialog from "../components/ApiDemoDialog.vue"
import AppDetailDrawer from "../components/AppDetailDrawer.vue"
import AppEditorDrawer from "../components/AppEditorDrawer.vue"
import FilterSeg from "../components/FilterSeg.vue"
import { ElMessage } from "element-plus"
import { computed, h, onMounted, ref } from "vue"

import {
  createApp,
  disableApp,
  getApp,
  listApps,
  revokeOldAppKey,
  rotateAppKey,
  rotateCallbackSecret,
  updateApp,
  type AppPayload,
  type ManagedApp,
} from "../api/apps"
import { listConfigs } from "../api/admin"
import { getReport, type ReportRow } from "../api/reports"
import { useLatestRead } from "../composables/useLatestRead"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"

import LoadErrorAlert from "../components/LoadErrorAlert.vue"
import {
  callbackDisplay,
  graceHoursLeft,
  quotaPercent as quotaPercentOf,
  quotaTone as quotaToneOf,
} from "../lib/appDisplay"
import { copyText } from "../lib/clipboard"
import { useConfirmActions } from "../lib/confirm"
const { confirmAuditedAction, captureCurrent } = useConfirmActions()
import { formatNumber } from "../lib/format"
import { roleNames, type MessageCategory } from "../lib/labels"
import { formatDateTime, shanghaiDateKey } from "../lib/time"
import { errorText } from "../lib/error"

type SecretOperation = "create-app" | "rotate-api-key" | "rotate-callback-secret"

const CATEGORY_FILTERS: { label: string; value: MessageCategory | "all" }[] = [
  { label: "全部", value: "all" },
  ...CATEGORY_OPTIONS,
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
const categoryFilter = ref<MessageCategory | "all">("all")
const statusFilter = ref<"all" | "1" | "0">("all")
const detailId = ref<number | null>(null)
const detailOpen = ref(false)
const drawerOpen = ref(false)
const editorDrawer = ref<InstanceType<typeof AppEditorDrawer> | null>(null)
const secretOpen = ref(false)
const secretTitle = ref("")
const secretValue = ref("")
const secretHint = ref("")
const keyGraceHours = ref<number | null>(null)
const secretOperation = ref<SecretOperation | null>(null)
const rotatingKeyId = ref<number | null>(null)
const rotatingCallbackId = ref<number | null>(null)
// 行级动作在途守卫：进入即置位（confirm 之前），拦截确认框期间的重复点击。
const revokingKeyId = ref<number | null>(null)
const statusBusyId = ref<number | null>(null)
/** 今日用量联查结果（dim_value = app.id 字符串）；完整联查成功前单元格显示「—」。 */
const dailyUsage = ref<Map<string, ReportRow>>(new Map())
const usageRead = useLatestRead()
const listRead = useLatestRead()
const usageUnavailable = ref(true)

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

function quotaPercent(app: ManagedApp): number | null {
  return quotaPercentOf(consumedOf(app), app.daily_quota)
}

function quotaTone(app: ManagedApp): "" | "warn" | "over" {
  return quotaToneOf(quotaPercent(app))
}

const demoOpen = ref(false)
const demoApp = ref<ManagedApp | null>(null)
function openDemo(item: ManagedApp): void {
  demoApp.value = item
  demoOpen.value = true
}

async function load(): Promise<void> {
  const signal = listRead.start()
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listApps(signal)
    if (!signal.aborted) items.value = result
  } catch (error) {
    if (!signal.aborted) errorMessage.value = errorText(error, "应用列表加载失败")
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

/** 今日用量按应用维度读取有界分页；完整读完后发布，失败不把部分结果显示为完整用量。 */
async function loadDailyUsage(): Promise<void> {
  const signal = usageRead.start()
  const today = shanghaiDateKey()
  try {
    const usage = new Map<string, ReportRow>()
    const pageSize = 100
    let expectedTotal: number | undefined
    let currentPage = 1
    while (true) {
      const result = await getReport(
        {
          granularity: "day",
          groupBy: "app",
          category: "all",
          start: today,
          end: today,
        },
        { page: currentPage, size: pageSize },
        signal,
      )
      if (signal.aborted) return
      if (
        !Array.isArray(result.items) ||
        !Number.isSafeInteger(result.total) ||
        result.total < 0 ||
        result.size !== pageSize ||
        result.page !== currentPage
      ) {
        throw new Error("用量统计响应无效")
      }
      expectedTotal ??= result.total
      const expectedRows = Math.min(pageSize, expectedTotal - (currentPage - 1) * pageSize)
      if (result.total !== expectedTotal || result.items.length !== expectedRows) {
        throw new Error("用量统计分页不完整")
      }
      for (const row of result.items) {
        if (usage.has(row.dim_value)) throw new Error("用量统计分页存在重复应用")
        usage.set(row.dim_value, row)
      }
      if (currentPage * pageSize >= expectedTotal) break
      currentPage += 1
    }
    if (usage.size !== expectedTotal) throw new Error("用量统计分页不完整")
    dailyUsage.value = usage
    usageUnavailable.value = false
  } catch {
    if (signal.aborted) return
    dailyUsage.value = new Map()
    usageUnavailable.value = true
  }
}

const keyGraceRead = useLatestRead()
async function loadKeyGraceHours(): Promise<void> {
  const signal = keyGraceRead.start()
  try {
    const configs = await listConfigs(signal)
    if (signal.aborted) return
    const raw = configs.find((item) => item.key === "key_grace_hours")?.value
    const value = Number(raw)
    keyGraceHours.value = Number.isInteger(value) && value > 0 ? value : null
  } catch {
    if (signal.aborted) return
    keyGraceHours.value = null
    ElMessage.warning("密钥轮换宽限期读取失败，页面显示可能不完整")
  }
}

function openCreate(): void {
  editorDrawer.value?.prepare(null)
  drawerOpen.value = true
}

function openEdit(item: ManagedApp): void {
  editorDrawer.value?.prepare(item)
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

async function save(body: AppPayload, name: string, targetId: number | null): Promise<void> {
  const creating = targetId === null
  if (creating && secretOperation.value !== null) return
  if (creating) secretOperation.value = "create-app"
  let secretRevealed = false
  saving.value = true
  try {
    if (creating) {
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
      ElMessage.success("应用配置已更新 · 本次操作已记入审计")
    }
    drawerOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "应用保存失败"))
  } finally {
    saving.value = false
    if (creating && !secretRevealed) clearSecret()
  }
}

async function rotateKey(item: ManagedApp): Promise<void> {
  item = { ...item }
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-api-key"
  rotatingKeyId.value = item.id
  let secretRevealed = false
  try {
    const graceHint =
      keyGraceHours.value === null
        ? "旧 Key 将进入当前配置的宽限期。"
        : `旧 Key 将进入 ${keyGraceHours.value} 小时宽限期。`
    if (
      !(await confirmAuditedAction({
        title: "确认轮换 API Key",
        body: `将为 ${item.name} 生成新的 API Key。新 Key 仅展示一次，${graceHint}请确认已准备好立即复制并安全保存。`,
        auditNote: "轮换行为与操作人将写入审计日志。",
        confirmText: "确认轮换",
      }))
    )
      return
    const result = await rotateAppKey(item.id)
    secretRevealed = true
    reveal(
      "这是当前最终 API Key（仅展示一次）",
      result.api_key,
      `请立即复制并安全保存，确认保存后再关闭。旧 Key 宽限期至 ${formatDateTime(result.old_key_expires_at)}`,
    )
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "API Key 轮换失败"))
  } finally {
    if (!secretRevealed) clearSecret()
  }
}

async function revokeKey(item: ManagedApp): Promise<void> {
  item = { ...item }
  if (!item.old_key_prefix || !item.old_key_expires_at || revokingKeyId.value !== null) return
  revokingKeyId.value = item.id
  try {
    if (
      !(await confirmAuditedAction({
        title: "立即作废旧 Key？",
        body: `旧 Key ${item.old_key_prefix}•••• 原定 ${formatDateTime(item.old_key_expires_at)} 到期，作废后立即失效；仍使用旧 Key 的调用方将收到 401。`,
        auditNote: "作废行为与操作人将写入审计日志。",
        confirmText: "确认作废",
      }))
    )
      return
    await revokeOldAppKey(item.id)
    ElMessage.success("旧 API Key 已作废 · 本次操作已记入审计")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "作废失败"))
  } finally {
    revokingKeyId.value = null
  }
}

async function rotateCallback(item: ManagedApp): Promise<void> {
  item = { ...item }
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-callback-secret"
  rotatingCallbackId.value = item.id
  let secretRevealed = false
  try {
    if (
      !(await confirmAuditedAction({
        title: "确认轮换回调密钥",
        body: `将为 ${item.name} 生成新的回调密钥，已部署的旧密钥立即失效。新密钥仅展示一次，请确认已准备好立即复制并安全保存。`,
        auditNote: "轮换行为与操作人将写入审计日志。",
        confirmText: "确认轮换",
      }))
    )
      return
    const result = await rotateCallbackSecret(item.id)
    secretRevealed = true
    reveal("新回调密钥（仅展示一次）", result.callback_secret)
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "回调密钥轮换失败"))
  } finally {
    if (!secretRevealed) clearSecret()
  }
}

async function disable(item: ManagedApp): Promise<void> {
  item = { ...item }
  if (statusBusyId.value !== null) return
  statusBusyId.value = item.id
  try {
    if (
      !(await confirmAuditedAction({
        title: `停用应用 ${item.name}？`,
        body: h("ul", { class: "apps-conseq" }, [
          h("li", "当前与宽限期旧 API Key 立即吊销，发送/查询返回 401"),
          h("li", "在途批次继续到终态，历史数据保留可查"),
          h("li", "未终结的旧回调在同一事务隔离为不可重试"),
          h("li", "恢复需管理员在详情抽屉重新启用"),
        ]),
        auditNote: "操作记审计（app_disable）· 操作人写入审计主体",
        confirmText: "确认停用",
      }))
    )
      return
    await disableApp(item.id)
    ElMessage.success(`应用 ${item.name} 已停用 · 本次操作已记入审计`)
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "停用失败"))
  } finally {
    statusBusyId.value = null
  }
}

/** 启用不再从列表行拼全字段 PUT：先取权威配置再仅改 status，消除字段漂移写坏配置的风险。 */
async function enable(item: ManagedApp): Promise<void> {
  item = { ...item }
  if (statusBusyId.value !== null) return
  statusBusyId.value = item.id
  const isCurrent = captureCurrent()
  try {
    if (
      !(await confirmAuditedAction({
        title: "确认启用",
        body: `启用应用 ${item.name}？`,
        auditNote: "启用行为与操作人将写入审计日志。",
        confirmText: "确认启用",
      }))
    )
      return
    const current = await getApp(item.id)
    if (!isCurrent()) return
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
    ElMessage.success(`应用 ${item.name} 已启用 · 本次操作已记入审计`)
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "启用失败"))
  } finally {
    statusBusyId.value = null
  }
}

onMounted(() => {
  void load()
  void loadDailyUsage()
  void loadKeyGraceHours()
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
      <FilterSeg
        v-model="categoryFilter"
        :options="CATEGORY_FILTERS"
        button-testid-prefix="apps-category"
        label="类别筛选"
        data-testid="apps-category-seg"
      />
    </div>
    <div class="apps-fld">
      <span>状态</span>
      <FilterSeg
        v-model="statusFilter"
        :options="STATUS_FILTERS"
        button-testid-prefix="apps-status"
        label="状态筛选"
        data-testid="apps-status-seg"
      />
    </div>
    <span class="apps-filter-note">接口全量返回 · 前端过滤</span>
  </div>

  <LoadErrorAlert class="apps-alert" :message="errorMessage" @retry="load" />

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
              {{ formatNumber(consumedOf(row) ?? 0) }}
              <small>/ {{ row.daily_quota === 0 ? "不限量" : formatNumber(row.daily_quota) }}</small>
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
          <span class="apps-mono">{{ formatNumber(row.rate_limit_per_min) }}</span>
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
      <el-table-column label="状态" width="80" fixed="right">
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
      <span class="apps-foot-role">读写：{{ roleNames(["admin"]) }} · 今日消耗取自每日统计汇总</span>
    </footer>
  </section>

  <AppDetailDrawer
    v-model="detailOpen"
    :app="detail"
    :consumed="detail ? consumedOf(detail) : null"
    :rate="detail ? rateOf(detail) : null"
    :usage-unavailable="usageUnavailable"
    :rotating-key-id="rotatingKeyId"
    :revoking-key-id="revokingKeyId"
    :rotating-callback-id="rotatingCallbackId"
    :status-busy-id="statusBusyId"
    :secret-busy="secretOperation !== null"
    @edit="editFromDetail"
    @demo="openDemo"
    @rotate-key="rotateKey"
    @revoke-key="revokeKey"
    @rotate-callback="rotateCallback"
    @disable="disable"
    @enable="enable"
  />

  <AppEditorDrawer ref="editorDrawer" v-model="drawerOpen" :saving="saving" @save="save" />

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

  <ApiDemoDialog v-model="demoOpen" :app="demoApp" />
</template>
