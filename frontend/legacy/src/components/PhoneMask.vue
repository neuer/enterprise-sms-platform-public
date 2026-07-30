<script setup lang="ts">
defineProps<{ value: string }>()
</script>

<template>
  <span class="phone-mask" :aria-label="`掩码号码 ${value}`">{{ value }}</span>
</template>
