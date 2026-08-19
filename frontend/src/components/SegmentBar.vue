<script setup lang="ts">
import type { SegmentPart } from "../api/webMessages"

defineProps<{
  parts: SegmentPart[]
  nextHint?: string
}>()
</script>

<template>
  <div class="seg-viz" aria-label="服务端计费分段">
    <div class="seg-cells">
      <i
        v-for="(part, index) in parts"
        :key="index"
        class="segment-part"
        data-testid="segment-part"
        :class="{ part: part.partial, 'segment-fill': true, partial: part.partial }"
        :title="`第 ${index + 1} 段 · ${part.used}/${part.capacity} 字`"
      ></i>
      <i class="ghost segment-ghost" aria-hidden="true" :title="nextHint || '下一段'"></i>
    </div>
  </div>
</template>

<style scoped>
.seg-viz {
  margin: 4px 0 8px;
}

.seg-cells {
  display: flex;
  gap: 4px;
}

.seg-cells i {
  flex: 1;
  height: 16px;
  border-radius: 4px;
  background: var(--verdi);
}

.seg-cells i.part {
  background: repeating-linear-gradient(
    135deg,
    var(--verdi) 0 4px,
    rgba(14, 122, 99, 0.35) 4px 8px
  );
}

.seg-cells i.ghost {
  flex: 0.6;
  background: var(--sink);
  border: 1px dashed var(--hair);
}
</style>
