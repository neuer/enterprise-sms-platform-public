<script setup lang="ts">
import { rangeToIsoParams } from "../lib/time"

import { ElMessage } from "element-plus"

import { computed, onMounted, ref } from "vue"

import { useRouter } from "vue-router"

import EmptyState from "../components/EmptyState.vue"

import FilterSeg from "../components/FilterSeg.vue"

import ListPagination from "../components/ListPagination.vue"

import PhoneMask from "../components/PhoneMask.vue"

import { blacklistReply, listReplies, type ReplyDisposition, type ReplyItem } from "../api/replies"

import { usePagedList } from "../composables/usePagedList"

import { useConfirmActions } from "../lib/confirm"
const { confirmAction } = useConfirmActions()

import { errorText } from "../lib/error"

import { phoneProblem } from "../lib/phone"

import { formatDateTime } from "../lib/time"

import { useSessionStore } from "../stores/session"

const session = useSessionStore()
// 测试环境未安装路由时 useRouter 返回 undefined，跳转入口做空值守卫。
const router = useRouter()
const phone = ref("")
const range = ref<[Date, Date] | null>(null)
const disposition = ref<ReplyDisposition>("all")
const optingOutId = ref<number | null>(null)
const canOptout = computed(() => session.canWrite)
// 与服务端 Query(pattern=^1\d{10}$) 同一规则（硬性规则 8）；服务端仍为权威校验。
const OPT_OUT_RE = /^(TD|T|退订)$/i

const dispositionOptions: { label: string; value: ReplyDisposition }[] = [
  { label: "全部", value: "all" },
  { label: "待加黑退订", value: "pending_optout" },
  { label: "已加黑", value: "blacklisted" },
]

const {
  items,
  total,
  page,
  loading,
  errorMessage,
  load,
  search: listSearch,
  reset,
} = usePagedList({
  fetcher: (page) =>
    listReplies({
      phone: phone.value.trim() || undefined,
      ...rangeToIsoParams(range.value),
      disposition: disposition.value,
      page,
    }),
  errorMessage: "回复列表加载失败",
  resetFilters: () => {
    phone.value = ""
    range.value = null
    disposition.value = "all"
  },
})

function isOptOutContent(content: string): boolean {
  return OPT_OUT_RE.test(content.trim())
}

/** 手机号即时校验提示：空或合法为 undefined，非法时表单内联展示。 */
const phoneError = computed(() => phoneProblem(phone.value.trim()))

const filtering = computed(() => Boolean(phone.value.trim()) || Boolean(range.value) || disposition.value !== "all")
const emptyState = computed(() =>
  filtering.value
    ? {
        title: "没有符合筛选条件的回复",
        description: "调整回复时间、手机号或处置口径后重新查询，也可重置筛选查看全部回复。",
      }
    : {
        title: "尚未采集到上行回复",
        description: "厂商回复轮询约每 5 分钟运行一次；也可按时间或手机号缩小查询范围。",
      },
)

/** 查询前先做手机号即时校验；seg 点选与「查询」按钮共用同一入口。 */
function search(): void {
  if (phoneError.value) {
    ElMessage.warning(phoneError.value)
    return
  }
  listSearch()
}

function setDisposition(next: ReplyDisposition): void {
  disposition.value = next
  search()
}

/** 跳转批次列表并直达该批次详情抽屉（批次页消费 batch_no 查询参数）。 */
function openBatch(batchNo: string): void {
  void router?.push({ name: "batches", query: { batch_no: batchNo } })
}

async function optout(item: ReplyItem): Promise<void> {
  item = { ...item }
  if (
    !(await confirmAction({
      title: "退订加黑确认",
      body: `将回复号码 ${item.phone} 加入退订黑名单？加入后发送将自动剔除该号码；加黑行为与操作人将写入审计日志。`,
      confirmText: "加入黑名单",
    }))
  )
    return
  try {
    optingOutId.value = item.id
    await blacklistReply(item.id)
    ElMessage.success("已加入退订黑名单 · 本次操作已记入审计")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "退订加黑失败"))
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
      <p>厂商回复先以完整密文归档，再按掩码号码进入查询与退订处置；轮询约每 5 分钟采集一次。</p>
    </div>
  </section>

  <form class="reply-filter-bar" @submit.prevent="search">
    <label class="reply-fld">
      <span>手机号精确查询</span>
      <el-input
        v-model="phone"
        class="reply-filter-phone"
        data-testid="reply-filter-phone"
        placeholder="输入 11 位手机号"
        maxlength="11"
        clearable
        inputmode="numeric"
      />
      <small v-if="phoneError" class="reply-phone-error">{{ phoneError }}</small>
    </label>
    <label class="reply-fld">
      <span>回复时间（可选）</span>
      <el-date-picker
        v-model="range"
        type="datetimerange"
        format="YYYY-MM-DD HH:mm"
        popper-class="qingluan-date-popper"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        range-separator="至"
        class="reply-filter-dates"
      />
    </label>
    <div class="reply-fld">
      <span>处置</span>
      <FilterSeg
        :model-value="disposition"
        :options="dispositionOptions"
        data-testid="reply-disposition-seg"
        button-testid-prefix="reply-disposition"
        aria-label="处置"
        @update:model-value="setDisposition"
      />
    </div>
    <div class="reply-filter-go">
      <el-button data-testid="reply-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="reply-reset" @click="reset">重置</el-button>
    </div>
    <p class="reply-privacy"
      >查询参数不进入 Nginx/Uvicorn 访问日志；服务端仅向 SQL 传递
      <code>phone_hmac</code> 候选。本页无解密端点，号码恒以掩码展示。</p
    >
  </form>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

  <section class="reply-results">
    <el-table v-loading="loading" :data="items" row-key="id" class="reply-table">
      <el-table-column label="回复时间" width="178">
        <template #default="{ row }"
          ><time class="mono-time">{{ formatDateTime(row.reply_time) }}</time></template
        >
      </el-table-column>
      <el-table-column label="回复号码" width="145">
        <template #default="{ row }"><PhoneMask :value="row.phone" /></template>
      </el-table-column>
      <el-table-column label="用户原文" min-width="260">
        <template #default="{ row }"
          ><span class="reply-content" :class="{ 'is-optout': isOptOutContent(row.content) }">{{
            row.content
          }}</span></template
        >
      </el-table-column>
      <el-table-column label="关联批次" min-width="190">
        <template #default="{ row }">
          <button
            v-if="row.batch_no"
            type="button"
            class="batch-code reply-batch-link"
            :title="`查看批次 ${row.batch_no}`"
            @click="openBatch(row.batch_no)"
            >{{ row.batch_no }}</button
          >
          <span v-else class="reply-unlinked-group">
            <el-tag type="warning" effect="plain">未关联</el-tag>
            <small class="reply-unlinked">未匹配到平台批次</small>
          </span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="96">
        <template #default="{ row }">
          <el-tag v-if="row.blacklisted" type="info" effect="plain">已加黑</el-tag>
          <span v-else class="reply-status-none">—</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canOptout" label="处置" width="108" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="!row.blacklisted"
            link
            type="danger"
            :loading="optingOutId === row.id"
            :data-testid="`reply-optout-${row.id}`"
            @click="optout(row)"
            >退订加黑</el-button
          >
          <span v-else class="reply-status-none">—</span>
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
          <time class="mono-time">{{ formatDateTime(item.reply_time) }}</time>
        </header>
        <p class="reply-content" :class="{ 'is-optout': isOptOutContent(item.content) }">{{ item.content }}</p>
        <footer>
          <button
            v-if="item.batch_no"
            type="button"
            class="batch-code reply-batch-link"
            :title="`查看批次 ${item.batch_no}`"
            @click="openBatch(item.batch_no)"
            >{{ item.batch_no }}</button
          >
          <span v-else class="reply-unlinked-group">
            <el-tag type="warning" effect="plain">未关联</el-tag>
            <small class="reply-unlinked">未匹配到平台批次</small>
          </span>
          <el-tag v-if="item.blacklisted" type="info" effect="plain">已加黑</el-tag>
          <el-button
            v-else-if="canOptout"
            link
            type="danger"
            :loading="optingOutId === item.id"
            :data-testid="`reply-mobile-optout-${item.id}`"
            @click="optout(item)"
            >退订加黑</el-button
          >
        </footer>
      </article>
      <EmptyState v-if="!loading && !items.length" :title="emptyState.title" :description="emptyState.description" />
    </div>

    <ListPagination v-model:page="page" :total="total" @change="load" />
  </section>
</template>
