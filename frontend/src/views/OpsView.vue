<script setup lang="ts">
import { ref, reactive, toRefs, watch, nextTick, onMounted } from "vue"
import { useRoute, useRouter } from "vue-router"
import CallbackView from "./CallbackView.vue"
import { useOpsQueue } from "../composables/useOpsQueue"
import OpsAlertsTab from "./ops/OpsAlertsTab.vue"
import OpsRawTab from "./ops/OpsRawTab.vue"
import OpsUncertainTab from "./ops/OpsUncertainTab.vue"
import OpsUnmatchedTab from "./ops/OpsUnmatchedTab.vue"
import OpsJobsTab from "./ops/OpsJobsTab.vue"
import OpsQueueTab from "./ops/OpsQueueTab.vue"
import OpsOutboxTab from "./ops/OpsOutboxTab.vue"

type TabName = "alerts" | "callbacks" | "raw" | "uncertain" | "unmatched" | "jobs" | "queue" | "outbox"

const OPS_TABS: TabName[] = ["alerts", "callbacks", "raw", "uncertain", "unmatched", "jobs", "queue", "outbox"]

const OPS_TAB_ITEMS: { name: TabName; label: string }[] = [
  { name: "alerts", label: "告警" },
  { name: "callbacks", label: "回调任务" },
  { name: "raw", label: "原始报文" },
  { name: "uncertain", label: "结果未知" },
  { name: "unmatched", label: "无主报告" },
  { name: "jobs", label: "任务健康" },
  { name: "queue", label: "队列恢复" },
  { name: "outbox", label: "Outbox 投递" },
]

const route = useRoute()

const router = useRouter()

function tabFromQuery(raw: unknown): TabName | null {
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== "string") return null
  return OPS_TABS.includes(value as TabName) ? (value as TabName) : null
}

const activeTab = ref<TabName>(tabFromQuery(route.query.tab) ?? "alerts")

const visitedTabs = ref<TabName[]>([activeTab.value])

function selectTab(tab: TabName): void {
  activeTab.value = tab
}

async function moveTab(direction: -1 | 1): Promise<void> {
  const current = OPS_TABS.indexOf(activeTab.value)
  const next = (current + direction + OPS_TABS.length) % OPS_TABS.length
  activeTab.value = OPS_TABS[next]
  await nextTick()
  document.getElementById(`ops-tab-${activeTab.value}`)?.focus()
}
const queueState = reactive(useOpsQueue())
const { queueBlocked, queueRecovered, recover } = toRefs(queueState)
watch(activeTab, (value) => {
  if (!visitedTabs.value.includes(value)) visitedTabs.value.push(value)
  if (tabFromQuery(route.query.tab) === value) return
  const query = { ...route.query }
  if (value === "alerts") delete query.tab
  else query.tab = value
  void router.replace({ query })
})
watch(
  () => route.query.tab,
  (raw) => {
    const next = tabFromQuery(raw)
    if (next && next !== activeTab.value) activeTab.value = next
  },
)
onMounted(() => void queueState.load())
</script>
<template>
  <section class="page-heading ops-heading">
    <div
      ><p class="eyebrow">CONTROL ROOM / 运维控制室</p><h1>运维中心</h1
      ><p>所有恢复动作均有守卫与审计；原始报文只展示无 PII 元数据。</p></div
    >
    <span class="ops-mode"><i></i> 审计在线 · 安全元数据</span>
  </section>

  <section
    v-if="queueBlocked || queueRecovered"
    :class="['circuit-banner', { recovered: queueRecovered && !queueBlocked }]"
  >
    <span class="circuit-icon" aria-hidden="true">♨</span>
    <div>
      <strong>{{ queueRecovered && !queueBlocked ? "实时队列已恢复" : "余额熔断 · 实时队列暂缓" }}</strong>
      <p>{{
        queueRecovered && !queueBlocked
          ? "积压将按 QPS 令牌逐步入队，恢复动作已记入审计。"
          : "实时发送暂停；批量通道状态以队列恢复面板为准。"
      }}</p>
    </div>
    <div class="circuit-actions">
      <el-button @click="activeTab = 'queue'">查看余额与队列</el-button>
      <el-button v-if="queueBlocked" type="primary" @click="recover">已充值，恢复队列</el-button>
      <el-tag v-else type="success">已记入审计</el-tag>
    </div>
  </section>

  <nav class="ops-tabs" role="tablist" aria-label="运维中心模块">
    <button
      v-for="tab in OPS_TAB_ITEMS"
      :id="`ops-tab-${tab.name}`"
      :key="tab.name"
      type="button"
      role="tab"
      :aria-selected="activeTab === tab.name"
      :aria-controls="`ops-panel-${tab.name}`"
      :tabindex="activeTab === tab.name ? 0 : -1"
      :class="{ active: activeTab === tab.name }"
      @click="selectTab(tab.name)"
      @keydown.left.prevent="moveTab(-1)"
      @keydown.right.prevent="moveTab(1)"
      >{{ tab.label }}</button
    >
  </nav>

  <aside class="ops-rules" aria-label="运维守卫与数据边界">
    <div
      ><span>守卫与审计</span
      ><p
        >重放 / 手动触发 / 死信重推 / 队列恢复均二次确认并写审计；uncertain 禁止自动重发，仅 reconcile
        可按厂商报文修复；到期进入保守终态后须双人确认处置，重发只建新批次。</p
      ></div
    >
    <div
      ><span>PII 边界</span
      ><p
        >原始报文只展示无 PII 元数据；手机号明文仅经请求体提交、服务端立即转 HMAC
        精确查询，不写入日志与存储；明文导出需二次认证。</p
      ></div
    >
  </aside>

  <OpsAlertsTab
    v-if="visitedTabs.includes('alerts')"
    v-show="activeTab === 'alerts'"
    :active="activeTab === 'alerts'"
    @navigate="selectTab"
  />

  <section
    v-if="visitedTabs.includes('callbacks')"
    v-show="activeTab === 'callbacks'"
    id="ops-panel-callbacks"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-callbacks"
  >
    <CallbackView embedded />
  </section>

  <OpsRawTab v-if="visitedTabs.includes('raw')" v-show="activeTab === 'raw'" :active="activeTab === 'raw'" />

  <OpsUncertainTab
    v-if="visitedTabs.includes('uncertain')"
    v-show="activeTab === 'uncertain'"
    :active="activeTab === 'uncertain'"
  />

  <OpsUnmatchedTab
    v-if="visitedTabs.includes('unmatched')"
    v-show="activeTab === 'unmatched'"
    :active="activeTab === 'unmatched'"
  />

  <OpsJobsTab v-if="visitedTabs.includes('jobs')" v-show="activeTab === 'jobs'" :active="activeTab === 'jobs'" />

  <OpsQueueTab
    v-if="visitedTabs.includes('queue')"
    v-show="activeTab === 'queue'"
    :active="activeTab === 'queue'"
    :state="queueState"
  />

  <OpsOutboxTab
    v-if="visitedTabs.includes('outbox')"
    v-show="activeTab === 'outbox'"
    :active="activeTab === 'outbox'"
  />
</template>
