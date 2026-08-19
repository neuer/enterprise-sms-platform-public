<script setup lang="ts">
import "../styles/workspace.css"

import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import {
  getDashboard,
  type DashboardCategory,
  type DashboardChannelMonitor,
  type DashboardSnapshot,
} from "../api/dashboard"
import BalanceChart from "../components/BalanceChart.vue"
import ChannelMonitor from "../components/ChannelMonitor.vue"
import EmptyState from "../components/EmptyState.vue"
import TrendChart from "../components/TrendChart.vue"
import { jobDescription } from "../lib/jobDescriptions"

const snapshot = ref<DashboardSnapshot | null>(null)
const loading = ref(false)
const errorMessage = ref("")
const lastChannelSuccessAt = ref<string | null>(null)
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
const stalledJobs = computed(() => operations.value?.jobs.filter((item) => item.stalled) ?? [])
const healthyJobCount = computed(() => (operations.value?.jobs.length ?? 0) - stalledJobs.value.length)
const balancePollJob = computed(() => operations.value?.jobs.find((item) => item.job_name === "poll_balance") ?? null)
const balancePollLabel = computed(() => {
  const job = balancePollJob.value
  if (!job) return "尚未登记"
  const clock = job.last_run_at ? formatTime(job.last_run_at).slice(11, 16) : ""
  if (job.stalled) return clock ? `异常 · ${clock}` : "异常"
  if (!job.last_run_at) return "尚未运行"
  return `正常 · ${clock}`
})

/** 由 14 日余额快照推导日均消耗与预计可用天数；余额回升或样本不足时不估算。 */
const balanceStats = computed(() => {
  const ops = operations.value
  if (!ops || ops.balances.length < 2) return null
  const first = ops.balances[0]
  const last = ops.balances[ops.balances.length - 1]
  const days = (Date.parse(last.stat_date) - Date.parse(first.stat_date)) / 86_400_000
  const consumed = first.balance - last.balance
  if (days < 1 || consumed <= 0) return null
  const daily = consumed / days
  const runway = ops.current_balance === null ? null : Math.floor(ops.current_balance / daily)
  return { daily, runway }
})

const balanceRunwayLabel = computed(() => {
  if (!operations.value) return ""
  const stats = balanceStats.value
  if (stats === null) return "近 14 日消耗速率暂不可估算"
  const runway = stats.runway === null ? "" : ` · 预计可用约 ${stats.runway} 天`
  return `日均消耗 ≈ ${Math.round(stats.daily).toLocaleString()}${runway}`
})

function formatTime(value: string | null): string {
  if (!value) return "尚未运行"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

function channelMonitorError(reason: DashboardChannelMonitor["degraded_reason"]): string {
  if (reason === "snapshot_incomplete") return "Redis 运行快照字段不完整，信道指标暂不可用"
  return "Redis 控制快照暂不可用，信道指标已降级"
}

async function load(): Promise<void> {
  if (loading.value) return
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await getDashboard()
    const channelMonitor = result.operations?.channel_monitor
    if (channelMonitor && !channelMonitor.stale) {
      lastChannelSuccessAt.value = result.refreshed_at
    } else if (channelMonitor?.stale) {
      errorMessage.value = channelMonitorError(channelMonitor.degraded_reason)
    }
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
          channel_monitor: {
            ...snapshot.value.operations.channel_monitor,
            stale: true,
            degraded_reason: "redis_unavailable",
          },
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
    <div class="zone-label"><span>业务成果 · 今日</span></div>
    <section class="dashboard-metrics" aria-label="今日关键指标">
      <router-link to="/reports" class="metric-link" data-testid="metric-messages">
        <el-card shadow="never" class="metric-card primary">
          <span>今日消息</span>
          <span class="kpi-go">→ 报表</span>
          <strong>{{ totalMessages.toLocaleString() }}</strong>
          <small>{{ totalSegments.toLocaleString() }} 计费条</small>
          <div class="category-strip" aria-label="分类消息量">
            <span v-for="item in snapshot.categories" :key="item.category" :class="item.category" :style="{ flexGrow: Math.max(item.total, 1) }" :title="`${categoryLabels[item.category]} ${item.total}`"></span>
          </div>
          <p>{{ snapshot.categories.map(item => `${categoryLabels[item.category]} ${item.total}`).join(' · ') }}</p>
        </el-card>
      </router-link>
      <router-link to="/reports" class="metric-link" data-testid="metric-success">
        <el-card shadow="never" class="metric-card">
          <span>送达成功率</span>
          <span class="kpi-go">→ 报表</span>
          <strong>{{ (snapshot.overall_success_rate * 100).toFixed(1) }}%</strong>
          <small>delivered / (delivered + failed)</small>
          <div class="rate-rows" aria-label="分类目成功率">
            <div v-for="item in snapshot.categories" :key="item.category" class="rate-row">
              <span>{{ categoryLabels[item.category] }}</span>
              <div class="rate-track"><i :class="item.category" :style="{ width: `${Math.min(100, item.success_rate * 100)}%` }"></i></div>
              <b>{{ (item.success_rate * 100).toFixed(1) }}%</b>
            </div>
          </div>
        </el-card>
      </router-link>
      <router-link to="/approvals" class="metric-link" data-testid="metric-approvals">
        <el-card shadow="never" class="metric-card warning">
          <span>待审批</span>
          <span class="kpi-go">→ 审批</span>
          <strong>{{ snapshot.pending_approvals.toLocaleString() }}</strong>
          <small>当前权限范围</small><p>及时处理避免发送窗口顺延</p>
        </el-card>
      </router-link>
      <router-link v-if="operations" to="/reports" class="metric-link" data-testid="metric-balance">
        <el-card
          shadow="never"
          class="metric-card"
          :class="{ danger: operations.current_balance !== null && balanceThreshold !== null && operations.current_balance < balanceThreshold }"
        >
          <span>厂商余额</span>
          <span class="kpi-go">→ 余额</span>
          <strong>{{ operations.current_balance?.toLocaleString() ?? '—' }}</strong>
          <small>计费条 · {{ balanceThresholdLabel }}</small>
          <p class="balance-runway">{{ balanceRunwayLabel }}</p>
        </el-card>
      </router-link>
    </section>

    <section class="dashboard-main-grid" :class="{ 'single-column': !operations }">
      <el-card shadow="never" class="dashboard-panel trend-panel">
        <template #header><div class="panel-title"><div><strong>近 7 日发送趋势</strong><small>按类目 · 消息条数</small></div><router-link to="/reports" class="panel-jump">报表 →</router-link></div></template>
        <TrendChart v-if="snapshot.trend?.length" :points="snapshot.trend" />
        <div v-if="snapshot.trend?.length" class="trend-legend">
          <span><i class="verify"></i>验证码</span>
          <span><i class="notice"></i>通知</span>
          <span><i class="market"></i>营销</span>
        </div>
        <EmptyState v-else title="趋势暂不可用" description="统计聚合任务每日运行后生成趋势。" />
      </el-card>
      <el-card v-if="operations" shadow="never" class="dashboard-panel balance-panel">
        <template #header><div class="panel-title"><div><strong>厂商余额</strong><small>近 14 日 · 每日末值</small></div><router-link to="/reports" class="panel-jump">详情 →</router-link></div></template>
        <template v-if="operations.balances.length">
          <div class="balance-summary">
            <strong class="balance-now">{{ operations.current_balance?.toLocaleString() ?? '—' }}<small>计费条</small></strong>
            <BalanceChart :points="operations.balances" />
            <div class="balance-meta">
              <div><span>告警阈值</span><b>{{ balanceThreshold?.toLocaleString() ?? '—' }}</b></div>
              <div><span>日均消耗（14 日）</span><b>{{ balanceStats ? `≈ ${Math.round(balanceStats.daily).toLocaleString()}` : '—' }}</b></div>
              <div><span>预计可用</span><b :class="{ warn: balanceStats?.runway != null && balanceStats.runway <= 30 }">{{ balanceStats?.runway != null ? `≈ ${balanceStats.runway} 天` : '—' }}</b></div>
              <div><span>余额轮询</span><b :class="{ warn: balancePollJob?.stalled }">{{ balancePollLabel }}</b></div>
            </div>
          </div>
        </template>
        <EmptyState v-else title="尚无余额快照" description="余额轮询任务成功后会生成每日趋势。" />
      </el-card>
    </section>

    <template v-if="operations">
      <div class="zone-label"><span>运行健康 · 实时</span></div>
      <router-link
        to="/ops?tab=queue"
        class="channel-monitor-link"
        data-testid="channel-monitor-link"
      >
        <ChannelMonitor
          :realtime-queue="operations.channel_monitor.realtime_queue"
          :bulk-queue="operations.channel_monitor.bulk_queue"
          :qps-used="operations.channel_monitor.qps_used"
          :qps-rate="operations.channel_monitor.qps_rate"
          :reserved-realtime-qps="operations.channel_monitor.reserved_realtime_qps"
          :stale="operations.channel_monitor.stale"
          :degraded-reason="operations.channel_monitor.degraded_reason"
          :refreshed-at="snapshot.refreshed_at"
          :last-successful-at="lastChannelSuccessAt"
        />
      </router-link>

      <section class="ops-grid" aria-label="运行健康明细">
        <el-card shadow="never" class="dashboard-panel alert-panel">
          <template #header><div class="panel-title"><div><strong>今日告警</strong><small>仅展示标题，不暴露载荷</small></div><router-link to="/ops?tab=alerts" class="panel-jump">查看全部</router-link></div></template>
          <ul v-if="operations.alerts.length" class="alert-list">
            <li v-for="item in operations.alerts" :key="`${item.created_at}-${item.title}`"><i :class="item.level"></i><div><strong>{{ item.title }}</strong><time>{{ formatTime(item.created_at).slice(11) }}</time></div></li>
          </ul>
          <EmptyState v-else title="今日没有告警" description="新的异常、余额或任务告警会出现在这里。" />
        </el-card>

        <el-card shadow="never" class="dashboard-panel disposition-panel">
          <template #header><div class="panel-title"><div><strong>待处置</strong><small>需人工跟进的运行项</small></div><router-link to="/ops" class="panel-jump">运维台</router-link></div></template>
          <div class="disposition-rows">
            <router-link
              v-for="item in [
                { key: 'uncertain', tab: 'uncertain', label: 'uncertain', value: operations.dispositions.uncertain, note: '结果未知分片，禁止自动重发' },
                { key: 'unmatched', tab: 'unmatched', label: 'unmatched', value: operations.dispositions.unmatched, note: '无主回执，等待迁移对账' },
                { key: 'callback_dead', tab: 'callbacks', label: 'callback dead', value: operations.dispositions.callback_dead, note: '回调五次失败，需人工重推' },
              ]"
              :key="item.key"
              class="disposition-link"
              :to="`/ops?tab=${item.tab}`"
              :data-testid="`disposition-${item.key}`"
            >
              <div class="disposition-row" :class="{ active: item.value > 0 }">
                <strong>{{ item.value }}</strong><span>{{ item.label }}</span><small>{{ item.note }}</small>
              </div>
            </router-link>
          </div>
        </el-card>

        <el-card shadow="never" class="dashboard-panel jobs-panel">
          <template #header><div class="panel-title"><div><strong>任务健康</strong><small>超过预期间隔 ×2 或最近失败为异常</small></div><router-link to="/ops?tab=jobs" class="panel-jump">全部任务 →</router-link></div></template>
          <div class="jobs-sum">
            <b>{{ healthyJobCount }}<span> / {{ operations.jobs.length }}</span></b>
            <span>正常</span>
          </div>
          <article v-for="job in stalledJobs" :key="job.job_name" class="job-alert">
            <code>{{ job.job_name }}</code>
            <p>{{ jobDescription(job.job_name) }}</p>
            <time>最后运行 {{ formatTime(job.last_run_at) }}</time>
          </article>
          <p class="jobs-ok">{{ stalledJobs.length ? '其余任务均在预期间隔内运行；默认只显示异常项，不再平铺全部。' : '全部任务均在预期间隔内运行。' }}</p>
        </el-card>
      </section>
    </template>
  </div>

  <el-card v-else-if="!errorMessage" v-loading="loading" shadow="never" class="empty-panel dashboard-loading"><el-skeleton :rows="8" animated /></el-card>
</template>
