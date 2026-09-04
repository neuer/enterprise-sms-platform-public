<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, onMounted, ref } from "vue"

import {
  addBlacklist,
  deleteBlacklist,
  listBlacklist,
  type BlacklistItem,
  type BlacklistSource,
} from "../api/blacklist"
import EmptyState from "../components/EmptyState.vue"
import PhoneMask from "../components/PhoneMask.vue"
import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { PHONE_RE } from "../lib/phone"
import { formatDateTime } from "../lib/time"

const items = ref<BlacklistItem[]>([])
const total = ref(0)
const page = ref(1)
const sourceFilter = ref<BlacklistSource | "all">("all")
const keyword = ref("")
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")
const drawerOpen = ref(false)
const phonesText = ref("")
const remark = ref("")

// 与服务端 PHONE_PATTERN 同一规则（硬性规则 8）；服务端仍为权威校验。

const sourceOptions: { label: string; value: BlacklistSource | "all" }[] = [
  { label: "全部", value: "all" },
  { label: "人工加入", value: "manual" },
  { label: "回复退订", value: "reply_optout" },
  { label: "导入", value: "import" },
]

const sourceMeta: Record<BlacklistSource, { label: string; type: "primary" | "warning" | "info" }> = {
  manual: { label: "人工加入", type: "primary" },
  reply_optout: { label: "回复退订", type: "warning" },
  import: { label: "导入", type: "info" },
}

function sourceLabel(source: BlacklistSource): string {
  return sourceMeta[source]?.label ?? source
}

function sourceType(source: BlacklistSource): "primary" | "warning" | "info" {
  return sourceMeta[source]?.type ?? "info"
}

/** 与服务端 BlacklistService.add 同口径拆分：空白/中英文逗号分号分隔，行号即拆分序号。 */
const entries = computed(() =>
  phonesText.value.split(/[\s,，;；]+/).map((value) => value.trim()).filter(Boolean),
)

/** 格式错误行的 1 基序号；服务端 400 报错同样只带行号。 */
const invalidLines = computed(() =>
  entries.value.flatMap((value, index) => (PHONE_RE.test(value) ? [] : [index + 1])),
)

/** 批内按号码字符串去重（与服务端按 phone_hmac 归并同效），得到实际提交清单。 */
const uniquePhones = computed(() => [...new Set(entries.value.filter((value) => PHONE_RE.test(value)))])

const dupeCount = computed(() => entries.value.length - invalidLines.value.length - uniquePhones.value.length)

const invalidHint = computed(() => {
  if (!invalidLines.value.length) return ""
  const shown = invalidLines.value.slice(0, 5).join("、")
  const suffix = invalidLines.value.length > 5 ? ` 等共 ${invalidLines.value.length}` : ""
  return `第 ${shown}${suffix} 行格式错误，应为 11 位手机号；修正后才可提交。服务端报错同样只带行号。`
})

const canSubmit = computed(
  () => entries.value.length > 0 && invalidLines.value.length === 0 && !saving.value,
)

const filtering = computed(() => sourceFilter.value !== "all" || Boolean(keyword.value.trim()))
const emptyState = computed(() =>
  filtering.value
    ? { title: "没有符合筛选条件的记录", description: "调整来源或关键词后重新查询，也可重置筛选查看全部记录。" }
    : { title: "黑名单为空", description: "点击右上「添加号码」，或等待用户回复退订后，号码会出现在这里。" },
)

let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listBlacklist({
      source: sourceFilter.value === "all" ? "" : sourceFilter.value,
      keyword: keyword.value.trim(),
      page: page.value,
    })
    if (token !== loadToken) return
    items.value = result.items
    total.value = result.total
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "黑名单加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function search(): void {
  page.value = 1
  void load()
}

function reset(): void {
  sourceFilter.value = "all"
  keyword.value = ""
  page.value = 1
  void load()
}

/** 来源 seg 点选即重查，与上行回复页同一语言。 */
function setSource(next: BlacklistSource | "all"): void {
  if (next === sourceFilter.value) return
  sourceFilter.value = next
  search()
}

function openDrawer(): void {
  phonesText.value = ""
  remark.value = ""
  drawerOpen.value = true
}

async function add(): Promise<void> {
  if (!entries.value.length) {
    ElMessage.warning("请先输入要添加的手机号")
    return
  }
  if (invalidLines.value.length) {
    ElMessage.warning(invalidHint.value)
    return
  }
  saving.value = true
  try {
    const result = await addBlacklist(uniquePhones.value, remark.value.trim() || null)
    const updatedTip = result.updated ? ` · 已存在并更新 ${result.updated} 个` : ""
    ElMessage.success(`新增 ${result.added} 个${updatedTip} · 本次操作已记入审计`)
    drawerOpen.value = false
    page.value = 1
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "添加失败")
  } finally {
    saving.value = false
  }
}

async function remove(item: BlacklistItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "blacklist-delete-dialog" }, [
        h("p", `将 ${item.phone_mask} 移出黑名单？移出后通知与营销发送不再拦截该号码（验证码本就不拦截）。`),
        h("p", { class: "blacklist-delete-audit" }, "移除行为与操作人将写入审计日志；审计只记数量，不记号码。"),
      ]),
      "移出黑名单确认",
      {
        type: "warning",
        confirmButtonText: "移出黑名单",
        cancelButtonText: "取消",
        customClass: "blacklist-delete-box",
      },
    )
    await deleteBlacklist(item.phone_hmac)
    ElMessage.success("已移出黑名单 · 本次操作已记入审计")
    if (items.value.length === 1 && page.value > 1) page.value -= 1
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "移除失败")
    }
  }
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading blacklist-heading">
    <div>
      <p class="eyebrow">RECIPIENT GOVERNANCE / 接收治理</p>
      <h1>黑名单</h1>
      <p>营销强制拦截、通知默认拦截（应用可关）、验证码不拦截；号码仅持久化密文、HMAC 索引与掩码，本页无解密入口。</p>
    </div>
    <el-button data-testid="blacklist-add-open" type="primary" @click="openDrawer">添加号码</el-button>
  </section>

  <form class="blacklist-filter-bar" @submit.prevent="search">
    <div class="blacklist-fld">
      <span>来源</span>
      <div class="blacklist-seg" role="group" aria-label="来源筛选" data-testid="blacklist-source-seg">
        <button
          v-for="option in sourceOptions"
          :key="option.value"
          type="button"
          :class="{ on: sourceFilter === option.value }"
          :data-testid="`blacklist-source-${option.value}`"
          @click="setSource(option.value)"
        >{{ option.label }}</button>
      </div>
    </div>
    <label class="blacklist-fld">
      <span>关键词</span>
      <el-input
        v-model="keyword"
        class="blacklist-keyword"
        data-testid="blacklist-filter-keyword"
        placeholder="搜索掩码或备注"
        maxlength="64"
        clearable
        @clear="search"
      />
    </label>
    <div class="blacklist-filter-go">
      <el-button data-testid="blacklist-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="blacklist-reset" @click="reset">重置</el-button>
    </div>
    <p class="blacklist-privacy">关键词仅匹配掩码与备注（服务端 ILIKE，通配符已转义），不涉号码明文；变更经控制面锁串行并即时失效缓存，受理读下次重建。</p>
  </form>

  <el-alert v-if="errorMessage" class="blacklist-alert" :title="errorMessage" type="error" :closable="false" />

  <section class="blacklist-results">
    <el-table v-loading="loading" :data="items" row-key="phone_hmac" class="blacklist-table">
      <el-table-column label="号码" width="150">
        <template #default="{ row }"><PhoneMask :value="row.phone_mask" /></template>
      </el-table-column>
      <el-table-column label="来源" width="110">
        <template #default="{ row }"><el-tag :type="sourceType(row.source)" effect="plain">{{ sourceLabel(row.source) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="备注" min-width="200">
        <template #default="{ row }"><span :class="{ 'blacklist-dash': !row.remark }">{{ row.remark || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="加入时间" width="178">
        <template #default="{ row }"><time class="mono-time">{{ formatDateTime(row.created_at) }}</time></template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button :data-testid="`blacklist-delete-${row.phone_hmac.slice(0, 8)}`" link type="danger" @click="remove(row)">移除</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :title="emptyState.title" :description="emptyState.description" /></template>
    </el-table>

    <div v-loading="loading" class="blacklist-mobile-list">
      <article v-for="item in items" :key="item.phone_hmac">
        <header>
          <PhoneMask :value="item.phone_mask" />
          <el-tag :type="sourceType(item.source)" effect="plain" size="small">{{ sourceLabel(item.source) }}</el-tag>
        </header>
        <p>{{ item.remark || "无备注" }}</p>
        <footer>
          <time class="mono-time">{{ formatDateTime(item.created_at) }}</time>
          <el-button :data-testid="`mobile-blacklist-delete-${item.phone_hmac.slice(0, 8)}`" link type="danger" @click="remove(item)">移除</el-button>
        </footer>
      </article>
      <EmptyState v-if="!loading && !items.length" :title="emptyState.title" :description="emptyState.description" />
    </div>

    <footer class="blacklist-pagination">
      <span>共 {{ total }} 条 · 每页 20</span>
      <el-pagination
        v-model:current-page="page"
        data-testid="blacklist-pagination"
        :page-size="DEFAULT_PAGE_SIZE"
        :total="total"
        layout="prev, pager, next"
        @current-change="load"
      />
    </footer>
  </section>

  <el-drawer v-model="drawerOpen" class="blacklist-drawer" size="min(440px, 92vw)" :teleported="false">
    <template #header>
      <div class="blacklist-drawer-head">
        <div class="blacklist-drawer-title">添加号码到黑名单</div>
        <code>POST /api/v1/web/admin/blacklist · upsert</code>
      </div>
    </template>
    <el-form label-position="top" class="blacklist-form" @submit.prevent>
      <el-form-item>
        <template #label>
          号码
          <i class="blacklist-field-hint">每行一个，最多 50,000 个</i>
        </template>
        <el-input
          v-model="phonesText"
          class="blacklist-phones"
          data-testid="blacklist-phones"
          type="textarea"
          :rows="8"
          placeholder="每行一个手机号，如 13800000000"
        />
      </el-form-item>
      <div v-if="entries.length" class="blacklist-parse" data-testid="blacklist-parse">
        <span class="blacklist-chip blacklist-chip-ok">有效 {{ uniquePhones.length }}</span>
        <span v-if="dupeCount" class="blacklist-chip">批内去重 {{ dupeCount }}</span>
        <span v-if="invalidLines.length" class="blacklist-chip blacklist-chip-bad">格式错误 {{ invalidLines.length }}（第 {{ invalidLines.slice(0, 5).join("、") }} 行）</span>
      </div>
      <p v-if="invalidHint" class="blacklist-parse-error">{{ invalidHint }}</p>
      <el-form-item>
        <template #label>
          备注
          <i class="blacklist-field-hint">可选 · ≤128 字 · 不得含号码</i>
        </template>
        <el-input v-model="remark" data-testid="blacklist-remark" maxlength="128" placeholder="8 月投诉清单补录" />
      </el-form-item>
    </el-form>
    <template #footer>
      <div class="blacklist-editor-foot">
        <small>已存在号码将按全版本 HMAC 归并，以本次来源与备注更新；添加行为与数量写入审计日志。</small>
        <div>
          <el-button @click="drawerOpen = false">取消</el-button>
          <el-button data-testid="blacklist-add" type="primary" :disabled="!canSubmit" :loading="saving" @click="add">添加到黑名单</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
