<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage } from "element-plus"
import { computed, onMounted, ref } from "vue"

import {
  listCallbacks,
  retryCallback,
  type CallbackStatus,
  type CallbackTask,
} from "../api/callbacks"
import EmptyState from "../components/EmptyState.vue"

withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })

const items = ref<CallbackTask[]>([])
const total = ref(0)
const page = ref(1)
const status = ref<CallbackStatus | "">("")
const loading = ref(false)
const errorMessage = ref("")
const deadCount = computed(() => items.value.filter((item) => item.status === "dead").length)

const statusMeta: Record<CallbackStatus, { label: string; type: "warning" | "success" | "danger" | "info" }> = {
  pending: { label: "待投递", type: "info" },
  retrying: { label: "重试中", type: "warning" },
  done: { label: "已完成", type: "success" },
  dead: { label: "已死亡", type: "danger" },
}

function eventLabel(event: CallbackTask["event"]): string {
  return event === "batch.finished" ? "批次终态" : "明细报告"
}

function statusLabel(value: CallbackStatus): string {
  return statusMeta[value].label
}

function statusType(value: CallbackStatus): "warning" | "success" | "danger" | "info" {
  return statusMeta[value].type
}

function formatTime(value: string | null): string {
  if (!value) return "—"
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(new Date(value))
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listCallbacks(status.value, page.value)
    items.value = result.items
    total.value = result.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "回调任务加载失败"
  } finally {
    loading.value = false
  }
}

function filter(): void {
  page.value = 1
  void load()
}

async function retry(item: CallbackTask): Promise<void> {
  try {
    await retryCallback(item.id)
    ElMessage.success("回调任务已重新入队")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "手动重推失败")
  }
}

onMounted(load)
</script>

<template>
  <section v-if="!embedded" class="page-heading callback-heading">
    <div>
      <p class="eyebrow">DELIVERY TRACE / 投递轨迹</p>
      <h1>回调任务</h1>
      <p>仅展示状态与错误类型；目标 URL、签名密钥和消息 body 永不进入管理界面。</p>
    </div>
    <div class="callback-pulse" :class="{ danger: deadCount > 0 }">
      <span>本页 dead</span><strong>{{ deadCount }}</strong><small>/ {{ items.length }} 项</small>
    </div>
  </section>

  <el-card shadow="never" class="callback-filter-card">
    <div class="callback-filter filter-toolbar">
      <label for="callback-status">投递状态</label>
      <el-select id="callback-status" v-model="status" placeholder="全部状态" clearable @change="filter">
        <el-option label="待投递" value="pending" />
        <el-option label="重试中" value="retrying" />
        <el-option label="已完成" value="done" />
        <el-option label="已死亡" value="dead" />
      </el-select>
      <span>重试序列 60s → 5m → 15m → 1h → 1h</span>
    </div>
  </el-card>

  <el-card shadow="never" class="callback-table-card">
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="id" class="callback-table">
      <el-table-column label="任务 / 应用" min-width="168">
        <template #default="{ row }">
          <strong>{{ row.app_name }}</strong><small class="callback-id">CB-{{ row.id }}</small>
          <small class="callback-correlation" :title="row.correlation_id">RID {{ row.correlation_id }}</small>
        </template>
      </el-table-column>
      <el-table-column label="事件" width="112">
        <template #default="{ row }">{{ eventLabel(row.event) }}</template>
      </el-table-column>
      <el-table-column label="批次 / 引用" min-width="178">
        <template #default="{ row }">
          <code>{{ row.batch_no || "—" }}</code><small>{{ row.reference_count }} 条明细引用</small>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="108">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" :effect="row.status === 'dead' ? 'dark' : 'plain'">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="尝试" width="88">
        <template #default="{ row }"><span class="retry-count">{{ row.retry_count }}/5</span></template>
      </el-table-column>
      <el-table-column label="租约" min-width="132">
        <template #default="{ row }">
          <el-tag v-if="row.stalled" type="danger" effect="dark">已停滞</el-tag>
          <span v-else>{{ row.lease_id ? "执行中" : "—" }}</span>
          <small>接管 {{ row.takeover_count }} 次</small>
        </template>
      </el-table-column>
      <el-table-column label="最近结果" min-width="148">
        <template #default="{ row }">
          <span v-if="row.last_http_code" class="http-code">HTTP {{ row.last_http_code }}</span>
          <small>{{ row.last_error || "—" }}</small>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="178">
        <template #default="{ row }"><time>{{ formatTime(row.created_at) }}</time></template>
      </el-table-column>
      <el-table-column label="操作" width="104" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.status === 'dead'" link type="danger" @click="retry(row)">手动重推</el-button>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <template #empty><EmptyState title="当前没有回调任务" description="启用应用回调后，投递任务会出现在这里。" /></template>
    </el-table>

    <div class="callback-mobile-list">
      <article v-for="item in items" :key="item.id">
        <header>
          <strong>{{ item.app_name }} <small>CB-{{ item.id }} · RID {{ item.correlation_id }}</small></strong>
          <el-tag :type="statusType(item.status)" :effect="item.status === 'dead' ? 'dark' : 'plain'">
            {{ statusLabel(item.status) }}
          </el-tag>
        </header>
        <p>{{ eventLabel(item.event) }} · {{ item.batch_no || "未关联批次" }}</p>
        <dl>
          <div><dt>引用</dt><dd>{{ item.reference_count }}</dd></div>
          <div><dt>重试</dt><dd>{{ item.retry_count }}/5</dd></div>
          <div><dt>租约</dt><dd>{{ item.stalled ? "已停滞" : item.lease_id ? "执行中" : "—" }}</dd></div>
          <div><dt>接管</dt><dd>{{ item.takeover_count }}</dd></div>
          <div><dt>结果</dt><dd>{{ item.last_http_code ? `HTTP ${item.last_http_code}` : item.last_error || "—" }}</dd></div>
        </dl>
        <footer>
          <time>{{ formatTime(item.created_at) }}</time>
          <el-button v-if="item.status === 'dead'" link type="danger" @click="retry(item)">手动重推</el-button>
        </footer>
      </article>
      <EmptyState v-if="!items.length" title="当前没有回调任务" description="启用应用回调后，投递任务会出现在这里。" />
    </div>

    <footer class="callback-pagination">
      <span>共 {{ total }} 项</span>
      <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" />
    </footer>
  </el-card>
</template>
