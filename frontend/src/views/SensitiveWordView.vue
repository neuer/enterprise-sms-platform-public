<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, onMounted, ref } from "vue"

import { listConfigs, updateConfigs } from "../api/admin"
import {
  addSensitiveWords,
  deleteSensitiveWord,
  listSensitiveWords,
  type SensitiveWordItem,
} from "../api/sensitiveWords"
import EmptyState from "../components/EmptyState.vue"
import { formatDateTime } from "../lib/time"

const MAX_WORD_LENGTH = 64

const items = ref<SensitiveWordItem[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref("")
const policy = ref("block")
const loading = ref(false)
const saving = ref(false)
const policySaving = ref(false)
const errorMessage = ref("")
const drawerOpen = ref(false)
const wordsText = ref("")

const policyOptions = [
  { label: "命中阻断", value: "block" },
  { label: "仅审计", value: "audit" },
]

/** 与现提交口径一致拆分：换行/中英文逗号分号分隔，行号即拆分序号。 */
const entries = computed(() =>
  wordsText.value
    .split(/[\n,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean),
)

/** 超长词的 1 基序号；服务端 400 报错同样只带行号。 */
const oversizedLines = computed(() =>
  entries.value.flatMap((value, index) => (value.length > MAX_WORD_LENGTH ? [index + 1] : [])),
)

/** 批内按词面去重（与服务端 dict.fromkeys 归并同效），得到实际提交清单。 */
const uniqueWords = computed(() => [...new Set(entries.value.filter((value) => value.length <= MAX_WORD_LENGTH))])

const dupeCount = computed(() => entries.value.length - oversizedLines.value.length - uniqueWords.value.length)

const oversizedHint = computed(() => {
  if (!oversizedLines.value.length) return ""
  const shown = oversizedLines.value.slice(0, 5).join("、")
  const suffix = oversizedLines.value.length > 5 ? ` 等共 ${oversizedLines.value.length}` : ""
  return `第 ${shown}${suffix} 行敏感词超过 ${MAX_WORD_LENGTH} 字；修正后才可提交。服务端报错同样只带行号。`
})

const canSubmit = computed(() => entries.value.length > 0 && oversizedLines.value.length === 0 && !saving.value)

const filtering = computed(() => Boolean(keyword.value.trim()))
const emptyState = computed(() =>
  filtering.value
    ? { title: "没有符合筛选条件的记录", description: "调整关键词后重新查询，也可重置筛选查看全部词条。" }
    : { title: "敏感词库为空", description: "点击右上「添加敏感词」批量录入后，命中词将按当前策略阻断或仅审计。" },
)

let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const [pageResult, configs] = await Promise.all([
      listSensitiveWords({ keyword: keyword.value.trim(), page: page.value }),
      listConfigs(),
    ])
    if (token !== loadToken) return
    items.value = pageResult.items
    total.value = pageResult.total
    policy.value = configs.find((item) => item.key === "sensitive_hit_action")?.value || "block"
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "敏感词加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function search(): void {
  page.value = 1
  void load()
}

function reset(): void {
  keyword.value = ""
  page.value = 1
  void load()
}

/** 命中策略 seg 点选即写配置；失败回退原值。 */
async function setPolicy(next: string): Promise<void> {
  if (next === policy.value || policySaving.value) return
  const previous = policy.value
  policy.value = next
  policySaving.value = true
  try {
    await updateConfigs([{ key: "sensitive_hit_action", value: next }])
    ElMessage.success(next === "block" ? "敏感词命中将阻断发送" : "敏感词命中仅审计记录")
  } catch (error) {
    policy.value = previous
    ElMessage.error(error instanceof Error ? error.message : "策略更新失败")
  } finally {
    policySaving.value = false
  }
}

function openDrawer(): void {
  wordsText.value = ""
  drawerOpen.value = true
}

async function add(): Promise<void> {
  if (!entries.value.length) {
    ElMessage.warning("请先输入要添加的敏感词")
    return
  }
  if (oversizedLines.value.length) {
    ElMessage.warning(oversizedHint.value)
    return
  }
  saving.value = true
  try {
    const result = await addSensitiveWords(uniqueWords.value)
    const skippedTip = result.skipped ? ` · 已存在跳过 ${result.skipped} 个` : ""
    ElMessage.success(`新增 ${result.added} 个${skippedTip} · 本次操作已记入审计`)
    drawerOpen.value = false
    page.value = 1
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "添加失败")
  } finally {
    saving.value = false
  }
}

async function remove(item: SensitiveWordItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "sensitive-delete-dialog" }, [
        h(
          "p",
          `删除“${item.word}”？删除后该词不再参与命中判定（当前策略为${policy.value === "block" ? "命中阻断" : "仅审计"}，验证码 / 通知 / 营销全类别一致生效）。`,
        ),
        h("p", { class: "sensitive-delete-audit" }, "删除行为与操作人将写入审计日志；审计只记数量，不记词面。"),
      ]),
      "删除敏感词确认",
      {
        type: "warning",
        confirmButtonText: "删除敏感词",
        cancelButtonText: "取消",
        customClass: "sensitive-delete-box",
      },
    )
    await deleteSensitiveWord(item.id)
    ElMessage.success("已删除敏感词 · 本次操作已记入审计")
    if (items.value.length === 1 && page.value > 1) page.value -= 1
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "删除失败")
    }
  }
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading sensitive-heading">
    <div>
      <p class="eyebrow">CONTENT POLICY / 内容策略</p>
      <h1>敏感词</h1>
      <p
        >命中按当前策略阻断（422 SENSITIVE_WORD）或仅审计，验证码 / 通知 / 营销全类别一致执行；变更写库即重建
        Aho-Corasick 快照，其余进程按修订号自动加载。</p
      >
    </div>
    <div class="sensitive-head-ops">
      <div class="sensitive-policy" data-testid="sensitive-policy">
        <span>命中策略</span>
        <div class="sensitive-policy-seg" role="group" aria-label="命中策略">
          <button
            v-for="option in policyOptions"
            :key="option.value"
            type="button"
            :class="{ on: policy === option.value }"
            :data-testid="`sensitive-policy-${option.value}`"
            :disabled="policySaving"
            @click="setPolicy(option.value)"
            >{{ option.label }}</button
          >
        </div>
      </div>
      <el-button data-testid="sensitive-add-open" type="primary" @click="openDrawer">添加敏感词</el-button>
    </div>
  </section>

  <form class="sensitive-filter-bar" @submit.prevent="search">
    <label class="sensitive-fld">
      <span>关键词</span>
      <el-input
        v-model="keyword"
        class="sensitive-keyword"
        data-testid="sensitive-filter-keyword"
        placeholder="搜索敏感词"
        maxlength="64"
        clearable
        @clear="search"
      />
    </label>
    <div class="sensitive-filter-go">
      <el-button data-testid="sensitive-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="sensitive-reset" @click="reset">重置</el-button>
    </div>
    <p class="sensitive-filter-note"
      >关键词服务端 ILIKE 仅匹配词面（通配符已转义）；词库服务端分页过滤。词库变更经 sensitive_word_revision
      修订号广播，各进程下次匹配前自动重建快照。</p
    >
  </form>

  <el-alert v-if="errorMessage" class="sensitive-alert" :title="errorMessage" type="error" show-icon :closable="false"
    ><template #default><el-button link type="primary" @click="load">重新加载</el-button></template></el-alert
  >

  <section v-loading="loading" class="sensitive-results">
    <div v-if="items.length" class="sensitive-wall" data-testid="sensitive-wall">
      <el-tooltip
        v-for="item in items"
        :key="item.id"
        :content="`添加于 ${formatDateTime(item.created_at)}`"
        :disabled="!item.created_at"
        placement="top"
      >
        <span class="sensitive-tile">
          <span>{{ item.word }}</span>
          <button
            type="button"
            class="sensitive-tile-del"
            :data-testid="`sensitive-delete-${item.id}`"
            :aria-label="`删除 ${item.word}`"
            @click="remove(item)"
            >✕</button
          >
        </span>
      </el-tooltip>
    </div>
    <EmptyState v-else-if="!loading" :title="emptyState.title" :description="emptyState.description" />

    <footer class="sensitive-pagination">
      <span>共 {{ total }} 条 · 每页 60</span>
      <!-- 每页 60 为词条墙密度的刻意决策，见 api/sensitiveWords.ts -->
      <el-pagination
        v-model:current-page="page"
        data-testid="sensitive-pagination"
        :page-size="60"
        :total="total"
        layout="prev, pager, next"
        @current-change="load"
      />
    </footer>
  </section>

  <el-drawer v-model="drawerOpen" class="sensitive-drawer" size="min(440px, 92vw)" :teleported="false">
    <template #header>
      <div class="sensitive-drawer-head">
        <div class="sensitive-drawer-title">添加敏感词到词库</div>
        <code>POST /api/v1/web/admin/sensitive-words · 跳过已存在</code>
      </div>
    </template>
    <el-form label-position="top" class="sensitive-form" @submit.prevent>
      <el-form-item>
        <template #label>
          敏感词
          <i class="sensitive-field-hint">每行一个，最多 10,000 个，单词不超过 64 字</i>
        </template>
        <el-input
          v-model="wordsText"
          class="sensitive-words"
          data-testid="sensitive-words"
          type="textarea"
          :rows="8"
          placeholder="每行一个敏感词"
        />
      </el-form-item>
      <div v-if="entries.length" class="sensitive-parse" data-testid="sensitive-parse">
        <span class="sensitive-chip sensitive-chip-ok">有效 {{ uniqueWords.length }}</span>
        <span v-if="dupeCount" class="sensitive-chip">批内去重 {{ dupeCount }}</span>
        <span v-if="oversizedLines.length" class="sensitive-chip sensitive-chip-bad"
          >超长 {{ oversizedLines.length }}（第 {{ oversizedLines.slice(0, 5).join("、") }} 行）</span
        >
      </div>
      <p v-if="oversizedHint" class="sensitive-parse-error">{{ oversizedHint }}</p>
    </el-form>
    <template #footer>
      <div class="sensitive-editor-foot">
        <small>写库成功即重建全量快照，其余进程按修订号自动加载；已存在词跳过；添加行为与数量写入审计日志。</small>
        <div>
          <el-button @click="drawerOpen = false">取消</el-button>
          <el-button data-testid="sensitive-add" type="primary" :disabled="!canSubmit" :loading="saving" @click="add"
            >加入词库</el-button
          >
        </div>
      </div>
    </template>
  </el-drawer>
</template>
