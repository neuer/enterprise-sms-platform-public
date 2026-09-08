<script setup lang="ts">
import { toRefs, watch } from "vue"

import type { useOpsQueue } from "../../composables/useOpsQueue"

import type { UnwrapRef } from "vue"

const props = defineProps<{ active: boolean; state: UnwrapRef<ReturnType<typeof useOpsQueue>> }>()
const { queue, forceResume, loading, errorMessage, recover } = toRefs(props.state)
watch(
  () => props.active,
  (active) => {
    if (active) void props.state.load()
  },
)
</script>
<template>
  <div
    ><el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <section
      id="ops-panel-queue"
      v-loading="loading"
      class="ops-panel queue-recovery"
      role="tabpanel"
      aria-labelledby="ops-tab-queue"
    >
      <header class="ops-panel-title"
        ><div><strong>双队列恢复</strong><small>PostgreSQL 状态先恢复，Redis 仅作为投递通道</small></div></header
      >
      <template v-if="queue"
        ><div class="queue-status-grid"
          ><article
            ><span>REALTIME</span
            ><strong>{{ queue.realtime_code ? `暂停 · ${queue.realtime_code}` : "运行中" }}</strong></article
          ><article
            ><span>BULK</span><strong>{{ queue.bulk_code ? `暂停 · ${queue.bulk_code}` : "运行中" }}</strong></article
          ><article
            ><span>余额</span
            ><strong>{{ queue.balance === null ? "无快照" : `余额 ${queue.balance.toLocaleString()}` }}</strong
            ><small>阈值 {{ queue.threshold.toLocaleString() }}</small></article
          ></div
        ><div class="break-glass"
          ><el-switch
            v-model="forceResume"
            data-testid="force-resume"
            inline-prompt
            active-text="FORCE"
            inactive-text="SAFE"
          /><p>{{
            forceResume ? "将绕过余额与暂停原因守卫，操作会写审计。" : "仅余额达到阈值且暂停码为 999 时允许恢复。"
          }}</p
          ><el-button type="danger" @click="recover">恢复队列</el-button></div
        ></template
      >
    </section>
  </div>
</template>
