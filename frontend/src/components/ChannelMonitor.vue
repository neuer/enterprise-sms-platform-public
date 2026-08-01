<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

const props = withDefaults(defineProps<{
  realtimeQueue?: number | null
  bulkQueue?: number | null
  qpsUsed?: number | null
  qpsRate?: number | null
  reservedRealtimeQps?: number | null
  refreshedAt?: string | null
  lastSuccessfulAt?: string | null
  degradedReason?: "redis_unavailable" | "snapshot_incomplete" | null
  stale?: boolean
}>(), {
  realtimeQueue: null,
  bulkQueue: null,
  qpsUsed: null,
  qpsRate: null,
  reservedRealtimeQps: null,
  refreshedAt: null,
  lastSuccessfulAt: null,
  degradedReason: null,
  stale: true,
})

const clock = ref("")
let clockTimer: number | undefined

const realtimeLoad = computed(() => `${Math.min(100, (props.realtimeQueue ?? 0) * 2.2)}%`)
const bulkLoad = computed(() => `${Math.min(100, (props.bulkQueue ?? 0) / 60)}%`)
const usedTokens = computed(() => {
  if (props.qpsUsed === null || props.qpsRate === null || props.qpsRate <= 0) return 0
  const scaled = (props.qpsUsed / props.qpsRate) * 5
  const roundedUp = Math.trunc(scaled) + (Number.isInteger(scaled) ? 0 : 1)
  return Math.min(5, roundedUp)
})
const degradedMessage = computed(() => {
  if (props.degradedReason === "snapshot_incomplete") return "Redis 运行快照字段不完整"
  if (props.degradedReason === "redis_unavailable") return "Redis 运行快照读取失败（控制快照不可用）"
  return "Redis 运行快照暂不可用"
})

function displayNumber(value: number | null): string {
  return value === null ? "—" : value.toLocaleString()
}

function updateClock(): void {
  clock.value = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date())
}

function formatTimestamp(value: string | null): string {
  if (!value) return "尚无成功快照"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

onMounted(() => {
  updateClock()
  clockTimer = window.setInterval(updateClock, 1_000)
})

onBeforeUnmount(() => {
  if (clockTimer !== undefined) window.clearInterval(clockTimer)
})
</script>

<template>
  <section
    data-testid="channel-monitor"
    :class="['channel-monitor', { 'monitor-stale': stale }]"
    aria-label="短信信道实时监视"
  >
    <header class="monitor-header">
      <span class="monitor-live"><i aria-hidden="true"></i>{{ stale ? '数据暂不可用' : 'LIVE' }}</span>
      <time class="num">{{ clock }}</time>
    </header>

    <div class="monitor-lanes">
      <article class="monitor-lane realtime">
        <div><span>REALTIME / 实时通道</span><strong class="num">{{ displayNumber(realtimeQueue) }}</strong></div>
        <div class="monitor-track" aria-hidden="true"><i :style="{ width: realtimeLoad }"></i></div>
        <small>{{ realtimeQueue === null ? '队列深度暂不可用' : stale ? '上一成功快照' : '验证码与通知优先通道' }}</small>
      </article>

      <article class="monitor-lane bulk">
        <div><span>BULK / 批量通道</span><strong class="num">{{ displayNumber(bulkQueue) }}</strong></div>
        <div class="monitor-track" aria-hidden="true"><i :style="{ width: bulkLoad }"></i></div>
        <small>{{ bulkQueue === null ? '队列深度暂不可用' : stale ? '上一成功快照' : '营销批量通道' }}</small>
      </article>

      <article class="monitor-qps">
        <div><span>QPS TOKEN</span><strong class="num">{{ qpsUsed ?? '—' }} / {{ qpsRate ?? '—' }}</strong></div>
        <div class="token-grid" aria-label="QPS 令牌占用">
          <i v-for="index in 5" :key="index" :class="{ used: !stale && index <= usedTokens }"></i>
        </div>
        <small>{{ qpsUsed === null ? '令牌占用暂不可用' : stale ? '上一成功快照' : `实时通道预留 ${reservedRealtimeQps ?? 0} QPS` }}</small>
      </article>
    </div>

    <p v-if="stale" class="monitor-degraded">
      {{ degradedMessage }}；界面保留灰态并隐藏未知值，不伪造运行指标。
      <span>最近成功：{{ formatTimestamp(lastSuccessfulAt) }}；可点击顶部“刷新”重试。</span>
    </p>
    <span v-else-if="refreshedAt" class="monitor-refreshed num">最近更新 {{ formatTimestamp(refreshedAt) }}</span>
  </section>
</template>

<style scoped>
.channel-monitor {
  position: relative;
  padding: 18px 22px;
  overflow: hidden;
  color: var(--tx);
  background:
    radial-gradient(700px 140px at 88% -30%, rgba(18, 130, 104, 0.16), transparent 70%),
    linear-gradient(180deg, var(--panel), var(--panel-2));
  border: 1px solid var(--hair);
  border-radius: 12px;
}

.monitor-header,
.monitor-lane > div:first-child,
.monitor-qps > div:first-child {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.monitor-header {
  margin-bottom: 13px;
}

.monitor-live {
  display: inline-flex;
  gap: 7px;
  align-items: center;
  color: #71c4ad;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.16em;
}

.monitor-live i {
  width: 7px;
  height: 7px;
  background: var(--verdi-l);
  border-radius: 50%;
  box-shadow: 0 0 8px rgba(47, 161, 132, 0.6);
  animation: monitor-pulse 2.4s infinite;
}

.monitor-header time,
.monitor-refreshed {
  color: var(--tx-2);
  font-size: 10px;
}

.monitor-lanes {
  display: grid;
  grid-template-columns: 1fr 1fr 320px;
  gap: 28px;
}

.monitor-lane,
.monitor-qps {
  min-width: 0;
}

.monitor-lane span,
.monitor-qps span {
  color: #71c4ad;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
}

.monitor-lane.bulk span {
  color: var(--amber);
}

.monitor-lane strong,
.monitor-qps strong {
  color: var(--tx-hi);
  font-size: 21px;
  font-weight: 600;
}

.monitor-lane small,
.monitor-qps small {
  color: var(--tx-2);
  font-size: 10.5px;
}

.monitor-track {
  height: 10px;
  margin: 8px 0 6px;
  overflow: hidden;
  background: var(--sink);
  border: 1px solid var(--hair);
  border-radius: 5px;
}

.monitor-track i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--verdi), var(--verdi-l));
  transition: width 900ms cubic-bezier(0.4, 0, 0.2, 1);
  transform-origin: left;
}

.monitor-lane.bulk .monitor-track i {
  background: linear-gradient(90deg, #8a5309, var(--amber));
}

.monitor-qps {
  padding-left: 28px;
  border-left: 1px solid var(--hair);
}

.token-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
  margin: 8px 0 6px;
}

.token-grid i {
  height: 26px;
  background: var(--sink);
  border: 1px solid var(--hair);
  border-radius: 5px;
}

.token-grid i.used {
  background: linear-gradient(180deg, #12a17e, #0a5a49);
  box-shadow: 0 0 12px rgba(18, 161, 126, 0.4);
}

.monitor-degraded {
  margin: 13px 0 0;
  color: var(--tx-2);
  font-size: 10.5px;
}

.monitor-stale {
  filter: saturate(0.36);
}

.monitor-stale .monitor-live {
  color: var(--tx-2);
}

.monitor-stale .monitor-live i {
  background: var(--tx-3);
  box-shadow: none;
  animation: none;
}

.num {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

@keyframes monitor-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

@media (max-width: 1080px) {
  .monitor-lanes { grid-template-columns: 1fr 1fr; }
  .monitor-qps { grid-column: 1 / -1; padding: 14px 0 0; border-top: 1px solid var(--hair); border-left: 0; }
}

@media (max-width: 680px) {
  .channel-monitor { padding: 16px; }
  .monitor-lanes { grid-template-columns: 1fr; gap: 16px; }
  .monitor-qps { grid-column: auto; }
}
</style>
