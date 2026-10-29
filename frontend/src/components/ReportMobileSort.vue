<script setup lang="ts">
defineProps<{ prop: string; order: "ascending" | "descending" }>()
const emit = defineEmits<{ field: [value: string]; toggle: [] }>()

function onField(event: Event): void {
  emit("field", (event.target as HTMLSelectElement).value)
}
</script>

<template>
  <div class="report-mobile-sort">
    <label
      >排序
      <select data-testid="report-mobile-sort-field" aria-label="排序字段" :value="prop" @change="onField">
        <option value="period_start">周期</option>
        <option value="total">消息数</option>
        <option value="total_segments">受理计费估算</option>
        <option value="success_rate">成功率</option>
      </select>
    </label>
    <button type="button" data-testid="report-mobile-sort-order" @click="emit('toggle')">
      {{ order === "ascending" ? "升序" : "降序" }}
    </button>
  </div>
</template>
