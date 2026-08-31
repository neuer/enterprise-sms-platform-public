<script setup lang="ts">
import { computed } from "vue"

import { formatDateTime, formatHms } from "../lib/time"

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
const qpsTitle = computed(() => {
  if (props.reservedRealtimeQps === null) return undefined
  return `实时通道预留 ${props.reservedRealtimeQps} QPS`
})

function displayNumber(value: number | null): string {
  return value === null ? "—" : value.toLocaleString()
}
</script>

<template>
  <section
    data-testid="channel-monitor"
    :class="['channel-monitor', { 'monitor-stale': stale }]"
    aria-label="短信信道实时监视"
  >
    <span class="monitor-live"><i aria-hidden="true"></i>{{ stale ? '数据暂不可用' : 'LIVE' }}</span>

    <article class="monitor-lane realtime">
      <span>REALTIME / 实时通道</span>
      <strong class="num">{{ displayNumber(realtimeQueue) }}</strong>
      <div class="monitor-track" aria-hidden="true"><i :style="{ width: realtimeLoad }"></i></div>
    </article>

    <article class="monitor-lane bulk">
      <span>BULK / 批量通道</span>
      <strong class="num">{{ displayNumber(bulkQueue) }}</strong>
      <div class="monitor-track" aria-hidden="true"><i :style="{ width: bulkLoad }"></i></div>
    </article>

    <article class="monitor-qps" :title="qpsTitle">
      <div>
        <span>QPS TOKEN</span>
        <strong class="num">{{ qpsUsed ?? '—' }} / {{ qpsRate ?? '—' }}</strong>
      </div>
      <div class="token-grid" aria-label="QPS 令牌占用">
        <i v-for="index in 5" :key="index" :class="{ used: !stale && index <= usedTokens }"></i>
      </div>
    </article>

    <time class="chan-time num">最近更新 {{ formatHms(stale ? lastSuccessfulAt : refreshedAt) }}</time>

    <p v-if="stale" class="monitor-degraded">
      {{ degradedMessage }}；界面保留灰态并隐藏未知值，不伪造运行指标。
      <span>最近成功：{{ formatDateTime(lastSuccessfulAt, "尚无成功快照") }}；可点击顶部“刷新”重试。</span>
    </p>
  </section>
</template>

<style scoped>
.channel-monitor {
  display: grid;
  grid-template-columns: auto 1fr 1fr 190px auto;
  gap: 16px 24px;
  align-items: center;
  padding: 13px 20px;
  color: var(--tx);
  background: linear-gradient(180deg, var(--panel), var(--panel-2));
  border: 1px solid var(--hair);
  border-radius: 12px;
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

.monitor-lane,
.monitor-qps {
  min-width: 0;
}

.monitor-lane {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  column-gap: 8px;
  align-items: center;
}

.monitor-lane span,
.monitor-qps span {
  color: #71c4ad;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 500;
}

.monitor-lane.bulk span {
  color: var(--amber);
}

.monitor-lane strong,
.monitor-qps strong {
  color: var(--tx-hi);
  font-size: 16px;
  font-weight: 600;
}

.monitor-qps strong {
  font-size: 13px;
}

.monitor-track {
  grid-column: 1 / -1;
  height: 6px;
  margin-top: 5px;
  overflow: hidden;
  background: var(--sink);
  border: 1px solid var(--hair-2);
  border-radius: 3px;
}

.monitor-track i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--verdi), var(--verdi-l));
  transition: width 900ms cubic-bezier(0.4, 0, 0.2, 1);
}

.monitor-lane.bulk .monitor-track i {
  background: linear-gradient(90deg, #8a5309, var(--amber));
}

.monitor-qps > div:first-child {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.token-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 5px;
  margin-top: 5px;
}

.token-grid i {
  height: 14px;
  background: var(--sink);
  border: 1px solid var(--hair-2);
  border-radius: 4px;
}

.token-grid i.used {
  background: linear-gradient(180deg, #12a17e, #0a5a49);
  box-shadow: 0 0 10px rgba(18, 161, 126, 0.35);
}

.chan-time {
  color: var(--tx-3);
  font-size: 10px;
  white-space: nowrap;
}

.monitor-degraded {
  grid-column: 1 / -1;
  margin: 0;
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
  .channel-monitor {
    grid-template-columns: auto 1fr 1fr;
  }

  .monitor-qps,
  .chan-time {
    grid-column: 1 / -1;
  }
}

@media (max-width: 680px) {
  .channel-monitor {
    grid-template-columns: 1fr;
    padding: 14px 16px;
  }

  .monitor-qps,
  .chan-time {
    grid-column: auto;
  }
}
</style>
