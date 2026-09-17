<script setup lang="ts">
import { DEFAULT_PAGE_SIZE } from "../lib/labels"

/**
 * 列表分页页脚单点：计数文案（单位词经 unit 承载差异）+ el-pagination。
 * 手抄页脚即缺陷；布局变体（如图例、结果内筛选、轮询状态）经插槽与页面分片的变体类承载。
 */
withDefaults(
  defineProps<{
    page: number
    total: number
    pageSize?: number
    /** 计数单位词（条 / 项 / 个批次 / 名用户）。 */
    unit?: string
    /** el-pagination 的 data-testid。 */
    testid?: string
    /** false 时不渲染计数文案（统计报表明细等仅翻页场景）。 */
    showCount?: boolean
    /** 追加在计数文案后的片段（如「· dead 总计 3」）；自带前导空格拼接。 */
    countTail?: string
  }>(),
  { pageSize: DEFAULT_PAGE_SIZE, unit: "条", testid: undefined, showCount: true, countTail: undefined },
)

const emit = defineEmits<{ "update:page": [value: number]; change: [] }>()

function onPageChange(next: number): void {
  emit("update:page", next)
  emit("change")
}
</script>

<template>
  <footer class="list-pagination">
    <slot name="before" />
    <span v-if="showCount" class="list-pagination-count"
      >共 {{ total }} {{ unit }} · 每页 {{ pageSize }}{{ countTail ? ` ${countTail}` : "" }}</span
    >
    <slot />
    <el-pagination
      :current-page="page"
      :data-testid="testid"
      :page-size="pageSize"
      :total="total"
      layout="prev, pager, next"
      @current-change="onPageChange"
    />
    <slot name="after" />
  </footer>
</template>
