<script setup lang="ts">
import type { SegmentPart } from "../api/webMessages"

defineProps<{ parts: SegmentPart[] }>()
</script>

<template>
  <div class="segment-list" aria-label="服务端计费分段">
    <div
      v-for="(part, index) in parts"
      :key="index"
      class="segment-part"
      data-testid="segment-part"
    >
      <span
        class="segment-fill"
        :class="{ partial: part.partial }"
        :style="{ width: `${(part.used / part.capacity) * 100}%` }"
      ></span>
      <b>{{ index + 1 }}</b>
      <small>{{ part.used }} / {{ part.capacity }}</small>
    </div>
    <div class="segment-part segment-ghost" aria-hidden="true">
      <b>+1</b>
      <small>下一段</small>
    </div>
  </div>
</template>

<style scoped>
.segment-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(86px, 1fr));
  gap: 7px;
}

.segment-part {
  position: relative;
  display: flex;
  justify-content: space-between;
  min-height: 34px;
  padding: 8px 9px;
  overflow: hidden;
  color: var(--tx-2);
  font-family: "IBM Plex Mono", monospace;
  border: 1px solid var(--line-2);
  border-radius: 5px;
}

.segment-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: color-mix(in srgb, var(--verdi) 14%, transparent);
}

.segment-fill.partial {
  background: repeating-linear-gradient(
    135deg,
    color-mix(in srgb, var(--verdi) 24%, transparent) 0 4px,
    color-mix(in srgb, var(--verdi) 8%, transparent) 4px 8px
  );
}

.segment-part b,
.segment-part small {
  position: relative;
}

.segment-part b {
  color: var(--verdi);
}

.segment-ghost {
  border-style: dashed;
  opacity: 0.55;
}

.segment-ghost b {
  color: var(--tx-3);
}
</style>
