<script setup lang="ts">
import { computed } from "vue"

const props = withDefaults(defineProps<{
  realtimeQueue?: number | null
  bulkQueue?: number | null
  qpsUsed?: number | null
  qpsRate?: number | null
  reservedRealtimeQps?: number | null
  refreshedAt?: string | null
  stale?: boolean
}>(), {
  realtimeQueue: null,
  bulkQueue: null,
  qpsUsed: null,
  qpsRate: null,
  reservedRealtimeQps: null,
  refreshedAt: null,
  stale: true,
})

const qpsPercent = computed(() => {
  if (props.qpsUsed === null || props.qpsRate === null || props.qpsRate <= 0) return 0
  return Math.min(100, Math.round((props.qpsUsed / props.qpsRate) * 100))
})

function display(value: number | null): string {
  return value === null ? "—" : value.toLocaleString()
}
</script>

<template>
  <section
    data-testid="channel-monitor"
    :class="['classic-channel-monitor', { 'monitor-stale': stale }]"
    aria-label="短信信道运行监视"
  >
    <header>
      <div><span class="live-dot" aria-hidden="true"></span><strong>{{ stale ? '数据暂不可用' : '信道实时状态' }}</strong></div>
      <time>{{ refreshedAt || '无可用快照' }}</time>
    </header>
    <div class="channel-facts">
      <article>
        <span>实时队列</span>
        <strong>{{ display(realtimeQueue) }}</strong>
        <small>验证码与通知优先通道</small>
      </article>
      <article>
        <span>批量队列</span>
        <strong>{{ display(bulkQueue) }}</strong>
        <small>营销批量通道</small>
      </article>
      <article class="qps-fact">
        <span>QPS 令牌</span>
        <strong>{{ qpsUsed ?? '—' }} / {{ qpsRate ?? '—' }}</strong>
        <div class="qps-track" aria-hidden="true"><i :style="{ width: `${qpsPercent}%` }"></i></div>
        <small>{{ stale ? '令牌容量暂不可用' : `实时通道预留 ${reservedRealtimeQps ?? 0} QPS` }}</small>
      </article>
    </div>
    <p v-if="stale">Redis 运行快照不可用，保留灰态最后值且不伪造未知指标。</p>
  </section>
</template>

<style scoped>
.classic-channel-monitor {
  padding: 16px 18px;
  background: linear-gradient(135deg, var(--surface), var(--surface-soft));
  border: 1px solid var(--line);
  border-left: 4px solid var(--green);
  border-radius: 8px;
}

.classic-channel-monitor > header,
.classic-channel-monitor header > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.classic-channel-monitor header strong { font-size: 14px; }
.classic-channel-monitor header time { color: var(--muted); font: 11px/1.4 var(--mono); }
.live-dot { width: 8px; height: 8px; background: var(--green); border-radius: 50%; box-shadow: 0 0 0 4px color-mix(in srgb, var(--green) 15%, transparent); }
.channel-facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 14px; }
.channel-facts article { display: grid; gap: 5px; min-width: 0; padding: 12px 14px; background: var(--surface-soft); border: 1px solid var(--line); }
.channel-facts span { color: var(--muted); font-size: 12px; }
.channel-facts strong { font: 600 20px/1.2 var(--mono); }
.channel-facts small { color: var(--muted); }
.qps-track { height: 5px; overflow: hidden; background: var(--line); border-radius: 99px; }
.qps-track i { display: block; height: 100%; background: var(--green); }
.classic-channel-monitor > p { margin: 12px 0 0; color: var(--muted); font-size: 12px; }
.monitor-stale { filter: saturate(0.35); border-left-color: var(--muted); }
.monitor-stale .live-dot { background: var(--muted); box-shadow: none; }

@media (max-width: 760px) {
  .classic-channel-monitor > header { align-items: flex-start; }
  .channel-facts { grid-template-columns: 1fr; }
}
</style>
