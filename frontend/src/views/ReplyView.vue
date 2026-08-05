<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref } from "vue"

import PhoneMask from "../components/PhoneMask.vue"
import EmptyState from "../components/EmptyState.vue"
import { blacklistReply, listReplies, type ReplyItem } from "../api/replies"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()
const items = ref<ReplyItem[]>([])
const total = ref(0)
const page = ref(1)
const phone = ref("")
const range = ref<[Date, Date] | null>(null)
const loading = ref(false)
const errorMessage = ref("")
const optingOutId = ref<number | null>(null)
const canOptout = computed(() => session.role === "admin" || session.role === "operator")
// 与服务端 Query(pattern=^1\d{10}$) 同一规则（硬性规则 8）；服务端仍为权威校验。
const PHONE_RE = /^1\d{10}$/

const filtering = computed(() => Boolean(phone.value.trim()) || Boolean(range.value))
const emptyState = computed(() =>
  filtering.value
    ? {
        title: "没有符合筛选条件的回复",
        description: "调整回复时间或手机号后重新查询，也可重置筛选查看全部回复。",
      }
    : {
        title: "尚未采集到上行回复",
        description: "回复轮询每 5 分钟运行一次，也可按时间或手机号缩小查询范围。",
      },
)

function formatTime(value: string): string {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(new Date(value))
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listReplies({
      phone: phone.value.trim() || undefined,
      start: range.value?.[0].toISOString(),
      end: range.value?.[1].toISOString(),
      page: page.value,
    })
    items.value = result.items
    total.value = result.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "回复列表加载失败"
  } finally {
    loading.value = false
  }
}

function phoneProblem(value: string): string | null {
  return value === "" || PHONE_RE.test(value) ? null : "手机号须为 11 位以 1 开头的数字"
}

function search(): void {
  const issue = phoneProblem(phone.value.trim())
  if (issue) {
    ElMessage.warning(issue)
    return
  }
  page.value = 1
  void load()
}

function reset(): void {
  phone.value = ""
  range.value = null
  page.value = 1
  void load()
}

async function optout(item: ReplyItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将回复号码 ${item.phone} 加入退订黑名单？后续发送将自动剔除该号码。`,
      "退订加黑确认",
      { confirmButtonText: "加入黑名单", cancelButtonText: "取消", type: "warning" },
    )
  } catch {
    return
  }
  optingOutId.value = item.id
  try {
    await blacklistReply(item.id)
    ElMessage.success("已加入退订黑名单")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "退订加黑失败")
  } finally {
    optingOutId.value = null
  }
}

onMounted(load)
</script>

<template>
  <section class="page-heading reply-heading">
    <div>
      <p class="eyebrow">UPLINK / 用户回声</p>
      <h1>上行回复</h1>
      <p>厂商回复先以完整密文归档，再按掩码号码进入查询与退订处置。</p>
    </div>
    <div class="reply-total" aria-label="回复总数">
      <span>当前结果</span>
      <strong>{{ total }}</strong>
      <small>条回复</small>
    </div>
  </section>

  <el-card shadow="never" class="reply-filter-card">
    <el-form class="reply-filter filter-grid" label-position="top" @submit.prevent="search">
      <el-form-item class="filter-span-4" label="手机号精确查询">
        <el-input
          v-model="phone"
          data-testid="reply-filter-phone"
          placeholder="输入 11 位手机号"
          maxlength="11"
          clearable
          inputmode="numeric"
        />
      </el-form-item>
      <el-form-item class="filter-span-6" label="回复时间">
        <el-date-picker
          v-model="range"
          type="datetimerange"
          popper-class="qingluan-date-popper"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          range-separator="至"
        />
      </el-form-item>
      <el-form-item class="reply-filter-actions filter-actions filter-span-2">
        <el-button data-testid="reply-search" type="primary" :loading="loading" native-type="submit">查询</el-button>
        <el-button data-testid="reply-reset" @click="reset">重置</el-button>
      </el-form-item>
    </el-form>
    <p class="reply-filter-note">手机号仅在本次请求内计算 HMAC 索引，不进入查询日志或持久层。</p>
  </el-card>

  <el-card shadow="never" class="reply-table-card">
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="id" class="reply-table">
      <el-table-column label="回复时间" width="178">
        <template #default="{ row }"><time class="mono-time">{{ formatTime(row.reply_time) }}</time></template>
      </el-table-column>
      <el-table-column label="回复号码" width="145">
        <template #default="{ row }"><PhoneMask :value="row.phone" /></template>
      </el-table-column>
      <el-table-column label="用户原文" min-width="260">
        <template #default="{ row }"><span class="reply-content">{{ row.content }}</span></template>
      </el-table-column>
      <el-table-column label="关联批次" min-width="170">
        <template #default="{ row }">
          <code v-if="row.batch_no" class="batch-code">{{ row.batch_no }}</code>
          <el-tag v-else type="warning" effect="plain">未关联</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="canOptout" label="处置" width="108" fixed="right">
        <template #default="{ row }">
          <el-tag v-if="row.blacklisted" type="info" effect="plain">已加黑</el-tag>
          <el-button
            v-else
            link
            type="danger"
            :loading="optingOutId === row.id"
            :data-testid="`reply-optout-${row.id}`"
            @click="optout(row)"
          >退订加黑</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState :title="emptyState.title" :description="emptyState.description" />
      </template>
    </el-table>

    <div v-loading="loading" class="reply-mobile-list">
      <article v-for="item in items" :key="item.id" class="reply-mobile-item">
        <header>
          <PhoneMask :value="item.phone" />
          <time class="mono-time">{{ formatTime(item.reply_time) }}</time>
        </header>
        <p class="reply-content">{{ item.content }}</p>
        <footer>
          <code v-if="item.batch_no" class="batch-code">{{ item.batch_no }}</code>
          <el-tag v-else type="warning" effect="plain">未关联</el-tag>
          <el-tag v-if="item.blacklisted && canOptout" type="info" effect="plain">已加黑</el-tag>
          <el-button
            v-else-if="canOptout"
            link
            type="danger"
            :loading="optingOutId === item.id"
            :data-testid="`reply-mobile-optout-${item.id}`"
            @click="optout(item)"
          >退订加黑</el-button>
        </footer>
      </article>
      <EmptyState v-if="!items.length" :title="emptyState.title" :description="emptyState.description" />
    </div>

    <footer class="reply-pagination">
      <span>第 {{ page }} 页 · 每页 20 条</span>
      <el-pagination
        v-model:current-page="page"
        :page-size="20"
        :total="total"
        layout="prev, pager, next"
        @current-change="load"
      />
    </footer>
  </el-card>
</template>
