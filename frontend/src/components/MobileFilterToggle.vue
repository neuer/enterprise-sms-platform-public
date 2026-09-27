<script setup lang="ts">
/**
 * 手机检索条折叠开关单点：仅 760px 以下显示。父级表单挂 `filter-collapsible` 与折叠态
 * `is-collapsed`，折叠时只保留标了 `filter-keep` 的高频字段与查询按钮组，其余字段与注脚隐藏；
 * 桌面不受影响。activeCount 为被收起字段中已生效的条件数，避免用户忘记隐藏条件仍在生效。
 */
defineProps<{ collapsed: boolean; activeCount: number }>()
const emit = defineEmits<{ "update:collapsed": [value: boolean] }>()
</script>

<template>
  <button
    type="button"
    class="mobile-filter-toggle"
    :class="{ 'is-active': collapsed && activeCount > 0 }"
    :aria-expanded="!collapsed"
    data-testid="mobile-filter-toggle"
    @click="emit('update:collapsed', !collapsed)"
  >
    <span>{{ collapsed ? "更多条件" : "收起条件" }}</span>
    <b v-if="collapsed && activeCount > 0">{{ activeCount }}</b>
    <i aria-hidden="true">{{ collapsed ? "▾" : "▴" }}</i>
  </button>
</template>
