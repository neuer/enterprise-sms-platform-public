<script setup lang="ts">
/**
 * 列表/快照加载失败的统一错误条：错误标题 + 内联重试按钮。
 * 各页面不再各自拼装 el-alert，也不允许出现没有重试入口的加载失败。
 * 类名经属性透传到 el-alert 根节点，页面原有间距类（如 ops-alert）继续有效。
 */
defineProps<{ message: string; retryText?: string }>()
const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <el-alert v-if="message" :title="message" type="error" :closable="false">
    <template #default
      ><el-button link type="primary" @click="emit('retry')">{{ retryText ?? "重新加载" }}</el-button></template
    >
  </el-alert>
</template>
