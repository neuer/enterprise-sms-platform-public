<script setup lang="ts">
import "../styles/workspace.css"

import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import { getDashboard, type DashboardCategory, type DashboardSnapshot } from "../api/dashboard"
import BalanceChart from "../components/BalanceChart.vue"
import ChannelMonitor from "../components/ChannelMonitor.vue"
import EmptyState from "../components/EmptyState.vue"

const snapshot = ref<DashboardSnapshot | null>(null)
const loading = ref(false)
const errorMessage = ref("")
let refreshTimer: number | undefined

const categoryLabels: Record<DashboardCategory, string> = {
  verify: "验证码",
  notice: "通知",
  market: "营销",
}

const totalMessages = computed(() => snapshot.value?.categories.reduce((sum, item) => sum + item.total, 0) ?? 0)
const totalSegments = computed(() => snapshot.value?.categories.reduce((sum, item) => sum + item.total_segments, 0) ?? 0)
const operations = computed(() => snapshot.value?.operations)
const balanceThreshold = computed(() => operations.value?.balance_alert_threshold ?? null)
const balanceThresholdLabel = computed(() => balanceThreshold.value === null
  ? "告警阈值暂不可用"
  : `告警阈值 ${balanceThreshold.value.toLocaleString()}`)

function formatTime(value: string | null): string {
  if (!value) return "尚未运行"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

async function load(): Promise<void> {
  if (loading.value) return
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await getDashboard()
    snapshot.value = result
    if (result.operations) {
      window.dispatchEvent(new CustomEvent("sms:dashboard-balance", {
        detail: { currentBalance: result.operations.current_balance },
      }))
    }
  } catch (error) {
    if (snapshot.value?.operations) {
      snapshot.value = {
        ...snapshot.value,
        operations: {
          ...snapshot.value.operations,
          channel_monitor: { ...snapshot.value.operations.channel_monitor, stale: true },
        },
      }
    }
    errorMessage.value = error instanceof Error ? error.message : "仪表盘加载失败"
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  refreshTimer = window.setInterval(() => void load(), 10_000)
})

onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
})
</script>

<template>
  <section class="page-heading dashboard-heading">
    <div>
      <p class="eyebrow">OPERATIONS / 运行总览</p>
      <h1>仪表盘</h1>
      <p>业务统计遵循当前数据权限，运行信号为平台级摘要。</p>
    </div>
    <div class="dashboard-refresh">
      <time v-if="snapshot">最后刷新 {{ formatTime(snapshot.refreshed_at) }}</time>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>
  </section>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" show-icon :closable="false" class="dashboard-error">
    <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
  </el-alert>

  <div v-if="snapshot" v-loading="loading && !snapshot" class="dashboard-shell">
    <ChannelMonitor
      v-if="operations"
      :realtime-queue="operations.channel_monitor.realtime_queue"
      :bulk-queue="operations.channel_monitor.bulk_queue"
      :qps-used="operations.channel_monitor.qps_used"
      :qps-rate="operations.channel_monitor.qps_rate"
      :reserved-realtime-qps="operations.channel_monitor.reserved_realtime_qps"
      :stale="operations.channel_monitor.stale"
      :refreshed-at="snapshot.refreshed_at"
    />
    <section class="dashboard-metrics" aria-label="今日关键指标">
      <el-card shadow="never" class="metric-card primary">
        <span>今日消息</span><strong>{{ totalMessages.toLocaleString() }}</strong>
        <small>{{ totalSegments.toLocaleString() }} 计费条</small>
        <div class="category-strip" aria-label="分类消息量">
          <span v-for="item in snapshot.categories" :key="item.category" :class="item.category" :style="{ flexGrow: Math.max(item.total, 1) }" :title="`${categoryLabels[item.category]} ${item.total}`"></span>
        </div>
        <p>{{ snapshot.categories.map(item => `${categoryLabels[item.category]} ${item.total}`).join(' · ') }}</p>
      </el-card>
      <el-card shadow="never" class="metric-card">
        <span>送达成功率</span><strong>{{ (snapshot.overall_success_rate * 100).toFixed(1) }}%</strong>
        <small>delivered / (delivered + failed)</small><p>unknown / other 不进入分母</p>
      </el-card>
      <el-card shadow="never" class="metric-card warning">
        <span>待审批</span><strong>{{ snapshot.pending_approvals.toLocaleString() }}</strong>
        <small>当前权限范围</small><p>及时处理避免发送窗口顺延</p>
      </el-card>
      <el-card
        v-if="operations"
        shadow="never"
        class="metric-card"
        :class="{ danger: operations.current_balance !== null && balanceThreshold !== null && operations.current_balance < balanceThreshold }"
      >
        <span>厂商余额</span><strong>{{ operations.current_balance?.toLocaleString() ?? '—' }}</strong>
        <small>计费条</small><p>{{ balanceThresholdLabel }}</p>
      </el-card>
    </section>

    <section v-if="operations" class="dashboard-main-grid">
      <el-card shadow="never" class="dashboard-panel balance-panel">
        <template #header><div class="panel-title"><div><strong>余额走势</strong><small>最近 14 个自然日 · 每日末值</small></div><span>单位：计费条</span></div></template>
        <BalanceChart
          v-if="operations.balances.length"
          :points="operations.balances"
          :threshold="balanceThreshold"
        />
        <EmptyState v-else title="尚无余额快照" description="余额轮询任务成功后会生成每日趋势。" />
      </el-card>
      <el-card shadow="never" class="dashboard-panel alert-panel">
        <template #header><div class="panel-title"><div><strong>今日告警</strong><small>仅展示标题，不暴露载荷</small></div><span>{{ operations.alerts.length }} 条</span></div></template>
        <ul v-if="operations.alerts.length" class="alert-list">
          <li v-for="item in operations.alerts" :key="`${item.created_at}-${item.title}`"><i :class="item.level"></i><div><strong>{{ item.title }}</strong><time>{{ formatTime(item.created_at).slice(11) }}</time></div></li>
        </ul>
        <EmptyState v-else title="今日没有告警" description="新的异常、余额或任务告警会出现在这里。" />
      </el-card>
    </section>

    <section v-if="operations" class="disposition-grid" aria-label="待处置运行项">
      <el-card v-for="item in [
        { key: 'uncertain', label: 'uncertain', value: operations.dispositions.uncertain, note: '结果未知分片，禁止自动重发' },
        { key: 'unmatched', label: 'unmatched', value: operations.dispositions.unmatched, note: '无主回执，等待迁移对账' },
        { key: 'callback_dead', label: 'callback dead', value: operations.dispositions.callback_dead, note: '回调五次失败，需人工重推' },
      ]" :key="item.key" shadow="never" class="disposition-card" :class="{ active: item.value > 0 }">
        <span>{{ item.label }}</span><strong>{{ item.value }}</strong><p>{{ item.note }}</p>
      </el-card>
    </section>

    <el-card v-if="operations" shadow="never" class="dashboard-panel jobs-panel">
      <template #header><div class="panel-title"><div><strong>后台任务健康</strong><small>超过预期间隔 ×2 或最近失败显示红点</small></div><span>{{ operations.jobs.filter(item => !item.stalled).length }} / {{ operations.jobs.length }} 健康</span></div></template>
      <div class="job-grid">
        <article v-for="job in operations.jobs" :key="job.job_name" :class="['job-item', job.stalled && 'stalled']">
          <i :class="['job-dot', job.stalled ? 'danger' : 'healthy']"></i><div><code>{{ job.job_name }}</code><time>{{ formatTime(job.last_run_at) }}</time></div>
        </article>
      </div>
    </el-card>
  </div>

  <el-card v-else-if="!errorMessage" v-loading="loading" shadow="never" class="empty-panel dashboard-loading"><el-skeleton :rows="8" animated /></el-card>
</template>
