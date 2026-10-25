<script setup lang="ts">
defineProps<{
  open: boolean
  password: string
}>()

const emit = defineEmits<{
  "update:password": [value: string]
  confirm: []
  cancel: []
}>()
</script>

<template>
  <el-dialog
    :model-value="open"
    title="下载明文导出"
    width="min(420px, 92vw)"
    append-to-body
    destroy-on-close
    :close-on-click-modal="false"
    data-testid="export-password-dialog"
    @close="emit('cancel')"
  >
    <p>明文导出属于高风险操作，请重新输入当前认证源密码。</p>
    <el-input
      :model-value="password"
      type="password"
      show-password
      placeholder="当前密码"
      autocomplete="current-password"
      data-testid="export-password-input"
      @update:model-value="emit('update:password', $event)"
    />
    <template #footer>
      <el-button data-testid="export-password-cancel" @click="emit('cancel')">取消</el-button>
      <el-button data-testid="export-password-confirm" type="primary" @click="emit('confirm')">验证并下载</el-button>
    </template>
  </el-dialog>
</template>
