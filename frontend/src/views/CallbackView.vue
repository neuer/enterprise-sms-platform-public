<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, onMounted, reactive, ref } from "vue"

import { listApps, type ManagedApp } from "../api/apps"
import {
  listCallbacks,
  retryCallback,
  type CallbackEvent,
  type CallbackStatus,
  type CallbackTask,
} from "../api/callbacks"
import EmptyState from "../components/EmptyState.vue"
import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { formatDateTime } from "../lib/time"

withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })

const items = ref<CallbackTask[]>([])
const total = ref(0)
const deadTotal = ref(0)
const apps = ref<ManagedApp[]>([])
const loading = ref(false)
const errorMessage = ref("")
const retryingId = ref<number | null>(null)
const selected = ref<CallbackTask | null>(null)
const detailOpen = ref(false)
const filters = reactive({
  status: "" as CallbackStatus | "",
  appId: null as number | null,
  event: "" as CallbackEvent | "",
  batchNo: "",
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
})

const statusMeta: Record<CallbackStatus, { label: string; type: "warning" | "success" | "danger" | "info" }> = {
  pending: { label: "待投递", type: "info" },
  retrying: { label: "重试中", type: "warning" },
  done: { label: "已完成", type: "success" },
  dead: { label: "终止重试", type: "danger" },
}
const statusSegOptions = [
  { label: "全部", value: "" as CallbackStatus | "", key: "all" },
  { label: "待投递", value: "pending" as CallbackStatus, key: "pending" },
  { label: "重试中", value: "retrying" as CallbackStatus, key: "retrying" },
  { label: "已完成", value: "done" as CallbackStatus, key: "done" },
  { label: "终止重试", value: "dead" as CallbackStatus, key: "dead" },
]
const eventSegOptions = [
  { label: "全部", value: "" as CallbackEvent | "", key: "all" },
  { label: "批次终态", value: "batch.finished" as CallbackEvent, key: "batch" },
  { label: "明细报告", value: "message.report" as CallbackEvent, key: "report" },
]

const filtering = computed(() => Boolean(filters.status || filters.appId || filters.event || filters.batchNo.trim()))

function eventLabel(value: CallbackTask["event"]): string {
  return value === "batch.finished" ? "批次终态" : "明细报告"
}

function statusLabel(value: CallbackStatus): string {
  return statusMeta[value].label
}

function statusType(value: CallbackStatus): "warning" | "success" | "danger" | "info" {
  return statusMeta[value].type
}

let loadToken = 0

/** 抽屉选中行随每次重查同步：重推成功或筛选变化导致行离开当前列表时即收起抽屉。 */
function syncSelected(): void {
  if (!detailOpen.value || !selected.value) return
  const fresh = items.value.find((task) => task.id === selected.value!.id)
  if (fresh) {
    selected.value = fresh
  } else {
    detailOpen.value = false
  }
}

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listCallbacks({
      status: filters.status,
      appId: filters.appId,
      event: filters.event,
      batchNo: filters.batchNo,
      page: filters.page,
    })
    if (token !== loadToken) return
    items.value = result.items
    total.value = result.total
    deadTotal.value = result.dead_total
    syncSelected()
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "回调任务加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

async function loadApps(): Promise<void> {
  try {
    apps.value = await listApps()
  } catch {
    apps.value = []
    ElMessage.warning("应用列表加载失败，筛选可稍后重试")
  }
}

function search(): void {
  filters.page = 1
  void load()
}

function resetFilters(): void {
  filters.status = ""
  filters.appId = null
  filters.event = ""
  filters.batchNo = ""
  filters.page = 1
  void load()
}

/** 状态 / 事件 seg 点选即重查，与用户与角色、黑名单页同一语言。 */
function setStatus(value: CallbackStatus | ""): void {
  if (value === filters.status) return
  filters.status = value
  search()
}

function setEvent(value: CallbackEvent | ""): void {
  if (value === filters.event) return
  filters.event = value
  search()
}

function openDetail(item: CallbackTask): void {
  selected.value = item
  detailOpen.value = true
}

async function retry(item: CallbackTask): Promise<void> {
  if (retryingId.value !== null) return
  retryingId.value = item.id
  try {
    await ElMessageBox.confirm(
      h("div", { class: "callback-confirm-dialog" }, [
        h(
          "p",
          `将把 CB-${item.id}（${item.app_name} · ${eventLabel(item.event)}）重置为待投递并清零重试计数，dispatcher 随即按应用当前回调配置重新投递；应用已停用或回调 URL / 密钥已变更时重推将被拒绝。`,
        ),
        h("p", { class: "callback-confirm-audit" }, "重推行为、操作人与任务 id 将写入审计日志。"),
      ]),
      "确认手动重推",
      {
        confirmButtonText: "重推任务",
        cancelButtonText: "取消",
        type: "warning",
        customClass: "callback-confirm-box",
      },
    )
    await retryCallback(item.id)
    ElMessage.success("回调任务已重新入队 · 本次操作已记入审计")
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "手动重推失败")
    }
  } finally {
    retryingId.value = null
  }
}

onMounted(() => {
  void loadApps()
  void load()
})
</script>

<template>
  <section v-if="!embedded" class="page-heading callback-heading">
    <div>
      <p class="eyebrow">DELIVERY TRACE / 投递轨迹</p>
      <h1>回调任务</h1>
      <p>任务仅存事件引用与无 PII 元数据；目标 URL、签名密钥和消息 body 永不进入管理界面。写操作全部写入审计。</p>
    </div>
    <div class="callback-pulse" :class="{ danger: deadTotal > 0 }">
      <span>dead 总计</span><strong>{{ deadTotal }}</strong
      ><small>/ 列表 {{ total }} 项</small>
    </div>
  </section>

  <form class="callback-filter-bar" @submit.prevent="search">
    <div class="callback-fld">
      <span>投递状态</span>
      <div class="callback-seg" role="group" aria-label="投递状态筛选" data-testid="callback-status-seg">
        <button
          v-for="option in statusSegOptions"
          :key="option.key"
          type="button"
          :class="{ on: filters.status === option.value }"
          :data-testid="`callback-status-${option.key}`"
          @click="setStatus(option.value)"
          >{{ option.label }}</button
        >
      </div>
    </div>
    <div class="callback-fld">
      <span>事件</span>
      <div class="callback-seg" role="group" aria-label="事件筛选" data-testid="callback-event-seg">
        <button
          v-for="option in eventSegOptions"
          :key="option.key"
          type="button"
          :class="{ on: filters.event === option.value }"
          :data-testid="`callback-event-${option.key}`"
          @click="setEvent(option.value)"
          >{{ option.label }}</button
        >
      </div>
    </div>
    <div class="callback-fld">
      <span>应用</span>
      <el-select
        v-model="filters.appId"
        class="callback-app-select"
        placeholder="全部应用"
        clearable
        filterable
        data-testid="callback-app-filter"
        @change="search"
      >
        <el-option v-for="app in apps" :key="app.id" :label="app.name" :value="app.id" />
      </el-select>
    </div>
    <label class="callback-fld">
      <span>批次号</span>
      <el-input
        v-model="filters.batchNo"
        class="callback-keyword"
        data-testid="callback-batch-filter"
        clearable
        maxlength="32"
        placeholder="模糊匹配批次号"
        @clear="search"
      />
    </label>
    <div class="callback-filter-go">
      <el-button data-testid="callback-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="callback-reset" @click="resetFilters">重置</el-button>
    </div>
    <p class="callback-privacy"
      >状态 / 事件 / 应用点选即重查；批次号服务端 ILIKE 模糊匹配，通配符已转义。任务仅存事件引用与无 PII 元数据，目标
      URL、签名密钥与消息 body 不进入本页。</p
    >
  </form>

  <aside class="callback-rules" aria-label="重试、租约与重推规则">
    <div
      ><span>重试序列</span
      ><p
        >投递失败按 60s → 5m → 15m → 1h → 1h 重试，5 次后转 dead；间隔以 callback_retry_schedule 参数为准，beat
        启动时读取。</p
      ></div
    >
    <div
      ><span>租约接管</span><p>worker 租约停滞后由其他实例接管并累计接管次数；「已停滞」即当前租约超时未续约。</p></div
    >
    <div
      ><span>手动重推</span
      ><p>仅 dead 任务可重推：重置为待投递并清零重试计数，需应用启用且回调 URL / 密钥未变更。</p></div
    >
  </aside>

  <el-alert v-if="errorMessage" class="callback-alert" :title="errorMessage" type="error" show-icon :closable="false">
    <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
  </el-alert>

  <section class="callback-results">
    <template v-if="items.length || loading">
      <el-table v-loading="loading" :data="items" row-key="id" class="callback-table">
        <el-table-column label="任务 / 应用" min-width="168">
          <template #default="{ row }">
            <strong>{{ row.app_name }}</strong>
            <small class="callback-id">CB-{{ row.id }}</small>
          </template>
        </el-table-column>
        <el-table-column label="事件" width="96">
          <template #default="{ row }">{{ eventLabel(row.event) }}</template>
        </el-table-column>
        <el-table-column label="批次 / 引用" min-width="178">
          <template #default="{ row }">
            <code :title="row.batch_no || undefined">{{ row.batch_no || "—" }}</code>
            <small>{{ row.reference_count }} 条明细引用</small>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="108">
          <template #default="{ row }">
            <div class="callback-state">
              <el-tag :type="statusType(row.status)" :effect="row.status === 'dead' ? 'dark' : 'plain'">
                {{ statusLabel(row.status) }}
              </el-tag>
              <el-tag v-if="row.stalled" size="small" type="danger" effect="dark">已停滞</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="尝试 / 下次重试" width="142">
          <template #default="{ row }">
            <span class="retry-count">{{ row.retry_count }}/5</span>
            <small v-if="row.status === 'retrying' && row.next_retry_at">{{ formatDateTime(row.next_retry_at) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="最近结果" min-width="148">
          <template #default="{ row }">
            <span v-if="row.last_http_code" class="http-code">HTTP {{ row.last_http_code }}</span>
            <small>{{ row.last_error || "—" }}</small>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="178">
          <template #default="{ row }"
            ><time>{{ formatDateTime(row.created_at) }}</time></template
          >
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button :data-testid="`callback-detail-${row.id}`" link type="primary" @click="openDetail(row)"
              >详情</el-button
            >
            <el-button
              v-if="row.status === 'dead'"
              :data-testid="`callback-retry-${row.id}`"
              link
              type="danger"
              :loading="retryingId === row.id"
              @click="retry(row)"
              >手动重推</el-button
            >
          </template>
        </el-table-column>
      </el-table>

      <div class="callback-mobile-list">
        <article v-for="item in items" :key="item.id">
          <header>
            <strong
              >{{ item.app_name }} <small>CB-{{ item.id }}</small></strong
            >
            <el-tag :type="statusType(item.status)" :effect="item.status === 'dead' ? 'dark' : 'plain'">
              {{ statusLabel(item.status) }}
            </el-tag>
          </header>
          <p
            >{{ eventLabel(item.event) }} · {{ item.batch_no || "未关联批次"
            }}<template v-if="item.stalled"> · 已停滞</template></p
          >
          <dl>
            <div
              ><dt>引用</dt><dd>{{ item.reference_count }}</dd></div
            >
            <div
              ><dt>重试</dt><dd>{{ item.retry_count }}/5</dd></div
            >
            <div v-if="item.status === 'retrying' && item.next_retry_at"
              ><dt>下次重试</dt><dd>{{ formatDateTime(item.next_retry_at) }}</dd></div
            >
            <div
              ><dt>结果</dt
              ><dd>{{ item.last_http_code ? `HTTP ${item.last_http_code}` : item.last_error || "—" }}</dd></div
            >
          </dl>
          <footer>
            <time>{{ formatDateTime(item.created_at) }}</time>
            <span>
              <el-button
                :data-testid="`mobile-callback-detail-${item.id}`"
                link
                type="primary"
                @click="openDetail(item)"
                >详情</el-button
              >
              <el-button
                v-if="item.status === 'dead'"
                :data-testid="`mobile-callback-retry-${item.id}`"
                link
                type="danger"
                :loading="retryingId === item.id"
                @click="retry(item)"
                >手动重推</el-button
              >
            </span>
          </footer>
        </article>
      </div>
    </template>
    <div v-else-if="filtering" class="callback-empty-action">
      <EmptyState title="没有符合筛选的回调任务" description="调整状态、应用、事件或批次号后重新查询。" />
      <el-button data-testid="clear-callback-filters" @click="resetFilters">清除筛选</el-button>
    </div>
    <div v-else class="callback-empty-action">
      <EmptyState title="当前没有回调任务" description="启用应用回调后，投递任务会出现在这里。" />
    </div>

    <footer class="callback-pagination">
      <span>共 {{ total }} 项 · 每页 20 · dead 总计 {{ deadTotal }}</span>
      <el-pagination
        v-model:current-page="filters.page"
        :page-size="filters.pageSize"
        :total="total"
        layout="prev, pager, next"
        @current-change="load"
      />
    </footer>
  </section>

  <el-drawer v-model="detailOpen" size="min(440px, 92vw)" :teleported="false" class="callback-drawer">
    <template #header>
      <div class="callback-drawer-head">
        <div class="callback-drawer-title">回调任务详情</div>
        <code>CB-{{ selected?.id ?? "—" }} · GET /api/v1/web/admin/callbacks 行投影</code>
      </div>
    </template>
    <template v-if="selected">
      <section class="callback-subject">
        <span>{{ selected.app_name }}</span>
        <code>CB-{{ selected.id }} · {{ eventLabel(selected.event) }}</code>
        <small>{{ selected.batch_no || "未关联批次" }} · {{ selected.reference_count }} 条明细引用</small>
      </section>
      <dl class="callback-fact-grid">
        <div>
          <dt>状态</dt>
          <dd>
            <el-tag
              :type="statusType(selected.status)"
              :effect="selected.status === 'dead' ? 'dark' : 'plain'"
              size="small"
              >{{ statusLabel(selected.status) }}</el-tag
            >
            <el-tag v-if="selected.stalled" size="small" type="danger" effect="dark">已停滞</el-tag>
          </dd>
        </div>
        <div
          ><dt>尝试</dt><dd class="mono-id">{{ selected.retry_count }}/5</dd></div
        >
        <div
          ><dt>下次重试</dt
          ><dd>{{ selected.status === "retrying" ? formatDateTime(selected.next_retry_at) : "—" }}</dd></div
        >
        <div
          ><dt>最近结果</dt
          ><dd
            >{{ selected.last_http_code ? `HTTP ${selected.last_http_code}` : "—"
            }}<template v-if="selected.last_error"> · {{ selected.last_error }}</template></dd
          ></div
        >
        <div
          ><dt>租约</dt
          ><dd>{{ selected.lease_id ? `执行中 · 到期 ${formatDateTime(selected.lease_expires_at)}` : "—" }}</dd></div
        >
        <div
          ><dt>接管次数</dt><dd class="mono-id">{{ selected.takeover_count }}</dd></div
        >
        <div
          ><dt>事件 ID</dt><dd class="mono-id">{{ selected.event_id }}</dd></div
        >
        <div
          ><dt>关联 RID</dt><dd class="mono-id">{{ selected.correlation_id }}</dd></div
        >
        <div
          ><dt>创建时间</dt><dd>{{ formatDateTime(selected.created_at) }}</dd></div
        >
        <div
          ><dt>完成时间</dt><dd>{{ formatDateTime(selected.finished_at) }}</dd></div
        >
      </dl>
    </template>
    <template #footer>
      <div class="callback-editor-foot">
        <small>目标 URL、签名密钥与消息 body 不进入本页；重推行为与操作人写入审计日志。</small>
        <div>
          <el-button
            v-if="selected?.status === 'dead'"
            :data-testid="`drawer-callback-retry-${selected.id}`"
            type="danger"
            :loading="retryingId === selected.id"
            @click="retry(selected)"
            >手动重推</el-button
          >
        </div>
      </div>
    </template>
  </el-drawer>
</template>
