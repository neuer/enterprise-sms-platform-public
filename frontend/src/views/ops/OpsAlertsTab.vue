<script setup lang="ts">
import { rangeToIsoParams } from "../../lib/time"

import { usePagedList } from "../../composables/usePagedList"

import ListPagination from "../../components/ListPagination.vue"

import FilterSeg from "../../components/FilterSeg.vue"

import { computed, onMounted, ref, watch } from "vue"

import {
  getCurrentAlerts,
  listAlerts,
  type AlertItem,
  type CurrentAlertItem,
  type CurrentAlertSnapshot,
} from "../../api/ops"

import { errorText } from "../../lib/error"

import { useLatestRead } from "../../composables/useLatestRead"

import { DEFAULT_PAGE_SIZE } from "../../lib/labels"

import { formatDateTime } from "../../lib/time"

import EmptyState from "../../components/EmptyState.vue"

import { usePolling } from "../../composables/usePolling"

const props = defineProps<{ active: boolean }>()
type TabName = "alerts" | "callbacks" | "raw" | "uncertain" | "unmatched" | "jobs" | "queue" | "outbox"
const emit = defineEmits<{ navigate: [tab: TabName] }>()

type AlertMode = "current" | "history"

const alertMode = ref<AlertMode>("current")

const currentAlerts = ref<CurrentAlertSnapshot | null>(null)

const alertType = ref("")

const alertLevel = ref<"" | AlertItem["level"]>("")

const alertRange = ref<[Date, Date] | null>(null)

const selectedAlert = ref<AlertItem | null>(null)

const alertDetailVisible = ref(false)
const currentAlertPolling = usePolling(() => load(), {
  intervalMs: 60_000,
  enabled: computed(() => props.active && alertMode.value === "current"),
})

const ALERT_LEVEL_LABELS: Record<AlertItem["level"], string> = {
  info: "提示",
  warn: "警告",
  crit: "严重",
}

const ALERT_LEVEL_OPTIONS: { key: string; label: string; value: "" | AlertItem["level"] }[] = [
  { key: "all", label: "全部", value: "" },
  { key: "info", label: "提示", value: "info" },
  { key: "warn", label: "警告", value: "warn" },
  { key: "crit", label: "严重", value: "crit" },
]

const CURRENT_SOURCE_LABELS: Record<string, string> = {
  postgresql: "PostgreSQL 运行事实",
  control_redis: "control Redis",
  usage_projection: "用量投影巡检",
  balance: "厂商余额巡检",
}

const alertEmpty = computed(() =>
  alertType.value.trim() || alertLevel.value || alertRange.value
    ? { title: "没有符合筛选条件的告警", description: "调整告警类型、等级或时间范围后重新查询，也可重置筛选。" }
    : { title: "暂无告警记录", description: "告警渠道为空时仅落 alert_log 与日志，不产生外呼。" },
)

const currentCritCount = computed(() => currentAlerts.value?.items.filter((item) => item.level === "crit").length ?? 0)

const currentWarnCount = computed(() => currentAlerts.value?.items.filter((item) => item.level === "warn").length ?? 0)

const currentUnknownText = computed(
  () => currentAlerts.value?.unknown_sources.map((source) => CURRENT_SOURCE_LABELS[source] ?? source).join("、") ?? "",
)

function duration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} 天`
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${Math.max(0, Math.round(seconds / 60))} 分钟`
}

const alertList = usePagedList({
  fetcher: (page, signal) =>
    listAlerts(
      { page, alertType: alertType.value, level: alertLevel.value || undefined, ...rangeToIsoParams(alertRange.value) },
      signal,
    ),
  errorMessage: "运维数据加载失败",
})

const { items: alerts, total: alertTotal, page: alertPage } = alertList

const snapshotRead = useLatestRead()

const snapshotLoading = ref(false)

const snapshotError = ref("")
const loading = computed(() => (alertMode.value === "current" ? snapshotLoading.value : alertList.loading.value))
const errorMessage = computed(() =>
  alertMode.value === "current" ? snapshotError.value : alertList.errorMessage.value,
)
async function load(_tab?: string): Promise<void> {
  if (!props.active) return
  if (alertMode.value === "history") {
    snapshotRead.cancel()
    snapshotLoading.value = false
    await alertList.load()
    return
  }
  alertList.cancel()
  const signal = snapshotRead.start()
  snapshotLoading.value = true
  snapshotError.value = ""
  try {
    const result = await getCurrentAlerts(signal)
    if (!signal.aborted) currentAlerts.value = result
  } catch (error) {
    if (!signal.aborted) snapshotError.value = errorText(error, "运维数据加载失败")
  } finally {
    if (!signal.aborted) snapshotLoading.value = false
  }
}

function setAlertMode(mode: AlertMode): void {
  if (alertMode.value === mode) return
  alertMode.value = mode
  void load("alerts")
}
function reloadFromFirstPage(_tab?: string): void {
  alertPage.value = 1
  void load()
}

function setAlertLevel(value: "" | AlertItem["level"]): void {
  if (alertLevel.value === value) return
  alertLevel.value = value
  reloadFromFirstPage("alerts")
}

function resetAlerts(): void {
  alertType.value = ""
  alertLevel.value = ""
  alertRange.value = null
  reloadFromFirstPage("alerts")
}

function levelLabel(level: AlertItem["level"]): string {
  return ALERT_LEVEL_LABELS[level]
}

function levelTag(level: AlertItem["level"]): "danger" | "warning" | "info" {
  if (level === "crit") return "danger"
  if (level === "warn") return "warning"
  return "info"
}

function currentDuration(item: CurrentAlertItem): string {
  if (!item.since || !currentAlerts.value) return "起始时间未知"
  const seconds = Math.max(0, (Date.parse(currentAlerts.value.refreshed_at) - Date.parse(item.since)) / 1000)
  return `持续 ${duration(seconds)}`
}

function currentImpact(item: CurrentAlertItem): string {
  const detail = item.detail
  if (typeof detail.count === "number") return `${detail.count} 项`
  if (typeof detail.consecutive_failures === "number") return `连续 ${detail.consecutive_failures} 次`
  if (typeof detail.mismatched_dimensions === "number") {
    return `${detail.mismatched_dimensions} 个维度 · 差值 ${String(detail.absolute_delta ?? "—")}`
  }
  if (typeof detail.balance === "number") return `余额 ${detail.balance} / 阈值 ${String(detail.threshold ?? "—")}`
  if (typeof detail.dead === "number") return `活动 ${String(detail.active ?? 0)} · 死信 ${detail.dead}`
  const pauses = [
    detail.realtime_code && `实时 ${detail.realtime_code}`,
    detail.bulk_code && `批量 ${detail.bulk_code}`,
  ].filter(Boolean)
  if (pauses.length) return pauses.join(" · ")
  if (typeof detail.source === "string") return detail.source === "report" ? "状态报告" : "上行回复"
  return "—"
}
function goCurrentTarget(item: CurrentAlertItem): void {
  emit("navigate", item.target)
}

function openAlertDetail(item: AlertItem): void {
  selectedAlert.value = item
  alertDetailVisible.value = true
}
watch(
  () => props.active,
  (active) => {
    if (active) void load()
    else {
      alertList.cancel()
      snapshotRead.cancel()
      snapshotLoading.value = false
    }
  },
  { immediate: true },
)
onMounted(() => currentAlertPolling.start())
</script>
<template>
  <div>
    <el-alert v-if="errorMessage" class="ops-alert" :title="errorMessage" type="error" :closable="false"
      ><template #default><el-button link type="primary" @click="load()">重新加载</el-button></template></el-alert
    >
    <section
      id="ops-panel-alerts"
      v-loading="loading"
      class="ops-panel"
      role="tabpanel"
      aria-labelledby="ops-tab-alerts"
    >
      <header class="ops-panel-title ops-alert-title">
        <div
          ><strong>{{ alertMode === "current" ? "当前未恢复告警" : "告警触发历史" }}</strong
          ><small>{{
            alertMode === "current"
              ? "从权威运行事实实时计算，不按最近告警时间猜测"
              : "去重后的触发快照，不代表异常仍在持续或外部渠道已送达"
          }}</small></div
        >
        <FilterSeg
          :model-value="alertMode"
          :options="[
            ...[{ label: '当前告警', value: 'current' as const }],
            ...[{ label: '告警历史', value: 'history' as const }],
          ]"
          aria-label="告警视图"
          data-testid="ops-alert-mode"
          @update:model-value="setAlertMode"
        />
      </header>

      <template v-if="alertMode === 'current'">
        <el-alert
          v-if="currentAlerts && !currentAlerts.complete"
          data-testid="current-alert-incomplete"
          class="ops-alert"
          type="warning"
          :closable="false"
          title="当前告警状态不完整"
          :description="`以下来源暂时无法确认：${currentUnknownText}。未显示为正常，请先恢复数据源。`"
          show-icon
        />
        <div v-if="currentAlerts" class="ops-current-summary" data-testid="current-alert-summary">
          <div
            ><span>当前未恢复</span><strong>{{ currentAlerts.items.length }}</strong></div
          >
          <div
            ><span>严重</span><strong class="crit">{{ currentCritCount }}</strong></div
          >
          <div
            ><span>警告</span><strong class="warn">{{ currentWarnCount }}</strong></div
          >
          <p>刷新于 {{ formatDateTime(currentAlerts.refreshed_at) }} · 页面可见时每 60 秒更新</p>
        </div>
        <section class="ops-results">
          <el-table :data="currentAlerts?.items ?? []" row-key="key" class="ops-table">
            <el-table-column label="等级" width="88"
              ><template #default="{ row }"
                ><el-tag :type="levelTag(row.level)" :effect="row.level === 'crit' ? 'dark' : 'plain'">{{
                  levelLabel(row.level)
                }}</el-tag></template
              ></el-table-column
            >
            <el-table-column prop="title" label="当前问题" min-width="260" />
            <el-table-column label="影响" min-width="150"
              ><template #default="{ row }">{{ currentImpact(row) }}</template></el-table-column
            >
            <el-table-column prop="alert_type" label="类型" min-width="170" />
            <el-table-column label="持续状态" width="150"
              ><template #default="{ row }">{{ currentDuration(row) }}</template></el-table-column
            >
            <el-table-column label="最后确认" width="180"
              ><template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template></el-table-column
            >
            <el-table-column label="操作" width="90" fixed="right"
              ><template #default="{ row }"
                ><el-button
                  link
                  type="primary"
                  :data-testid="`current-alert-target-${row.key}`"
                  @click="goCurrentTarget(row)"
                  >去处理</el-button
                ></template
              ></el-table-column
            >
            <template #empty
              ><EmptyState
                :title="currentAlerts?.complete ? '当前没有未恢复告警' : '当前状态尚未完整确认'"
                :description="
                  currentAlerts?.complete
                    ? '所有已登记权威状态均为正常；历史触发仍可在“告警历史”查看。'
                    : '请先恢复上方列出的数据源，系统不会把未知状态显示为正常。'
                "
            /></template>
          </el-table>
          <div class="ops-mobile-list"
            ><article v-for="item in currentAlerts?.items ?? []" :key="item.key"
              ><header
                ><el-tag :type="levelTag(item.level)">{{ levelLabel(item.level) }}</el-tag
                ><time>{{ currentDuration(item) }}</time></header
              ><strong>{{ item.title }}</strong
              ><p>{{ currentImpact(item) }}</p
              ><p>{{ item.alert_type }} · {{ formatDateTime(item.checked_at) }}</p
              ><el-button link type="primary" @click="goCurrentTarget(item)">去处理</el-button></article
            ></div
          >
        </section>
      </template>

      <template v-else>
        <form class="ops-filter-bar" @submit.prevent="reloadFromFirstPage('alerts')">
          <label class="ops-fld"
            ><span>告警类型（精确）</span>
            <el-input
              v-model="alertType"
              class="ops-keyword"
              clearable
              placeholder="如 job_failed"
              aria-label="告警类型精确筛选"
            />
          </label>
          <div class="ops-fld"
            ><span>等级</span>
            <FilterSeg
              :model-value="alertLevel"
              :options="ALERT_LEVEL_OPTIONS"
              button-testid-prefix="ops-alert-level"
              aria-label="告警等级筛选"
              data-testid="ops-alert-level-seg"
              @update:model-value="setAlertLevel"
            />
          </div>
          <label class="ops-fld"
            ><span>时间范围</span>
            <el-date-picker
              v-model="alertRange"
              class="ops-dates"
              type="datetimerange"
              popper-class="qingluan-date-popper"
              range-separator="至"
              start-placeholder="开始时间"
              end-placeholder="结束时间"
            />
          </label>
          <div class="ops-filter-go">
            <el-button type="primary" @click="reloadFromFirstPage('alerts')">查询</el-button>
            <el-button @click="resetAlerts">重置</el-button>
          </div>
          <p class="ops-privacy"
            >服务端分页过滤；每条为去重后的触发快照，不表示异常仍在持续。“配置路由”也不等同于外部渠道已经送达。</p
          >
        </form>
        <section class="ops-results">
          <el-table :data="alerts" row-key="id" class="ops-table"
            ><el-table-column label="等级" width="88"
              ><template #default="{ row }"
                ><el-tag :type="levelTag(row.level)" :effect="row.level === 'crit' ? 'dark' : 'plain'">{{
                  levelLabel(row.level)
                }}</el-tag></template
              ></el-table-column
            ><el-table-column prop="title" label="告警" min-width="220" /><el-table-column
              prop="alert_type"
              label="类型"
              min-width="150" /><el-table-column prop="channels" label="配置路由" width="120" /><el-table-column
              label="记录时间"
              width="180"
              ><template #default="{ row }">{{ formatDateTime(row.created_at) }}</template></el-table-column
            ><el-table-column label="操作" width="70" fixed="right"
              ><template #default="{ row }"
                ><el-button link type="primary" :data-testid="`alert-detail-${row.id}`" @click="openAlertDetail(row)"
                  >详情</el-button
                ></template
              ></el-table-column
            ><template #empty><EmptyState :title="alertEmpty.title" :description="alertEmpty.description" /></template
          ></el-table>
          <div class="ops-mobile-list"
            ><article v-for="item in alerts" :key="item.id"
              ><header
                ><el-tag :type="levelTag(item.level)">{{ levelLabel(item.level) }}</el-tag
                ><time>{{ formatDateTime(item.created_at) }}</time></header
              ><strong>{{ item.title }}</strong
              ><p>{{ item.alert_type }} · {{ item.channels }}</p
              ><el-button link type="primary" @click="openAlertDetail(item)">详情</el-button></article
            ><EmptyState v-if="!alerts.length" :title="alertEmpty.title" :description="alertEmpty.description"
          /></div>
          <ListPagination
            v-model:page="alertPage"
            :total="alertTotal"
            :page-size="DEFAULT_PAGE_SIZE"
            testid="ops-alert-pagination"
            unit="条"
            @change="load('alerts')"
          ></ListPagination>
        </section>
      </template>
    </section>
    <el-drawer
      v-model="alertDetailVisible"
      title="告警详情"
      size="min(440px, 92vw)"
      :teleported="false"
      destroy-on-close
    >
      <template v-if="selectedAlert">
        <dl class="alert-detail-list">
          <div
            ><dt>等级</dt
            ><dd
              ><el-tag
                :type="selectedAlert.level === 'crit' ? 'danger' : selectedAlert.level === 'warn' ? 'warning' : 'info'"
                :effect="selectedAlert.level === 'crit' ? 'dark' : 'plain'"
                >{{ levelLabel(selectedAlert.level) }}</el-tag
              ></dd
            ></div
          >
          <div
            ><dt>类型</dt><dd>{{ selectedAlert.alert_type }}</dd></div
          >
          <div
            ><dt>渠道</dt><dd>{{ selectedAlert.channels }}</dd></div
          >
          <div
            ><dt>时间</dt><dd>{{ formatDateTime(selectedAlert.created_at) }}</dd></div
          >
        </dl>
        <h3 class="alert-detail-heading">{{ selectedAlert.title }}</h3>
        <pre v-if="selectedAlert.detail" class="alert-detail-json" data-testid="alert-detail-json">{{
          JSON.stringify(selectedAlert.detail, null, 2)
        }}</pre>
        <p v-else class="alert-detail-none">无附加详情</p>
      </template>
    </el-drawer>
  </div>
</template>
