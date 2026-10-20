<script setup lang="ts">
import { computed } from "vue"

import type { ManagedApp } from "../../api/apps"
import CategoryTag from "../../components/CategoryTag.vue"
import EmptyState from "../../components/EmptyState.vue"
import {
  callbackDisplay,
  graceHoursLeft,
  quotaPercent as quotaPercentOf,
  quotaTone as quotaToneOf,
} from "../../lib/appDisplay"
import { formatNumber } from "../../lib/format"
import { roleNames } from "../../lib/labels"

/** 应用管理列表与页脚：只展示页面推导好的结果并发出新建/详情意图，筛选、用量联查与写操作留在页面。 */
const props = defineProps<{
  /** 接口全量结果，仅用于区分「无应用」与「无匹配」空态。 */
  items: ManagedApp[]
  filtered: ManagedApp[]
  loading: boolean
  /** 一次性凭据流程在途时禁用新建。 */
  secretBusy: boolean
  /** 今日消耗（计费条）；联查不可用时返回 null。 */
  consumedOf: (app: ManagedApp) => number | null
}>()

const emit = defineEmits<{
  create: []
  detail: [app: ManagedApp]
}>()

const enabledCount = computed(() => props.filtered.filter((item) => item.status === 1).length)
const disabledCount = computed(() => props.filtered.length - enabledCount.value)

const emptyTitle = computed(() => (props.items.length === 0 ? "当前没有接入应用" : "没有符合筛选条件的应用"))
const emptyDescription = computed(() =>
  props.items.length === 0
    ? "创建应用后会得到一对 API Key / 回调密钥（仅展示一次）；类别、配额、限流与回调均可在详情中随时调整。"
    : "重置类别或状态筛选、清空关键词后查看全部应用。",
)

/** 停用应用整行降透明度，与密钥列「已随停用吊销」呼应。 */
function rowClassName({ row }: { row: ManagedApp }): string {
  return row.status ? "" : "apps-row-disabled"
}

function quotaPercent(app: ManagedApp): number | null {
  return quotaPercentOf(props.consumedOf(app), app.daily_quota)
}

function quotaTone(app: ManagedApp): "" | "warn" | "over" {
  return quotaToneOf(quotaPercent(app))
}
</script>

<template>
  <section class="apps-results">
    <el-table
      v-loading="loading"
      class="apps-table"
      data-testid="app-table"
      :data="filtered"
      row-key="id"
      :row-class-name="rowClassName"
    >
      <el-table-column label="应用" min-width="200">
        <template #default="{ row }">
          <span class="apps-name"
            >{{ row.name }}<span class="apps-name-id">#{{ row.id }}</span></span
          >
          <span class="apps-cell-sub">{{ row.dept }}</span>
        </template>
      </el-table-column>
      <el-table-column label="允许类别" min-width="150">
        <template #default="{ row }">
          <div class="apps-categories">
            <CategoryTag v-for="category in row.allowed_categories" :key="category" :category="category" />
          </div>
        </template>
      </el-table-column>
      <el-table-column label="今日消耗（计费条）" min-width="170">
        <template #default="{ row }">
          <div v-if="consumedOf(row) !== null" class="apps-quota-cell">
            <span class="apps-quota-num">
              {{ formatNumber(consumedOf(row) ?? 0) }}
              <small>/ {{ row.daily_quota === 0 ? "不限量" : formatNumber(row.daily_quota) }}</small>
            </span>
            <span v-if="quotaPercent(row) !== null" class="apps-quota-bar">
              <i :class="quotaTone(row)" :style="{ width: `${quotaPercent(row)}%` }"></i>
            </span>
          </div>
          <span v-else class="apps-cell-none">—</span>
        </template>
      </el-table-column>
      <el-table-column label="限流/分" width="90">
        <template #default="{ row }">
          <span class="apps-mono">{{ formatNumber(row.rate_limit_per_min) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="密钥" min-width="200">
        <template #default="{ row }">
          <div class="apps-key-cell">
            <template v-if="row.status === 1">
              <span class="apps-key-prefix">{{ row.api_key_prefix }}••••</span>
              <span v-if="graceHoursLeft(row) !== null" class="apps-key-tag apps-key-tag--grace">
                旧 Key 宽限 · 余 {{ graceHoursLeft(row) }}h
              </span>
              <span v-else class="apps-key-tag">单 Key 运行</span>
            </template>
            <span v-else class="apps-key-tag apps-key-tag--revoked">已随停用吊销</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="回调" min-width="170">
        <template #default="{ row }">
          <template v-if="row.callback_url">
            <span class="apps-callback-url" :title="row.callback_url">{{ callbackDisplay(row.callback_url) }}</span>
            <span class="apps-cell-sub">
              明细回调 {{ row.callback_report_enabled ? "开启" : "关闭" }} · 密钥{{
                row.callback_secret_configured ? "已配置" : "未配置"
              }}
            </span>
          </template>
          <span v-else class="apps-cell-none">未配置</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80" fixed="right">
        <template #default="{ row }">
          <el-tag :type="row.status ? 'success' : 'info'">{{ row.status ? "启用" : "停用" }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button :data-testid="`app-detail-${row.id}`" link type="primary" @click="emit('detail', row)"
            >详情</el-button
          >
        </template>
      </el-table-column>
      <template #empty>
        <div class="apps-empty">
          <EmptyState :title="emptyTitle" :description="emptyDescription" />
          <el-button
            v-if="!items.length"
            class="apps-empty-action"
            type="primary"
            :disabled="secretBusy"
            @click="emit('create')"
            >新建应用</el-button
          >
        </div>
      </template>
    </el-table>
    <footer class="apps-foot">
      <span>共 {{ filtered.length }} 个应用 · 启用 {{ enabledCount }} · 停用 {{ disabledCount }}</span>
      <span class="apps-foot-role">读写：{{ roleNames(["admin"]) }} · 今日消耗取自每日统计汇总</span>
    </footer>
  </section>
</template>
