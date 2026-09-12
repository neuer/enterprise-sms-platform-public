<script setup lang="ts">
import type { AdminStepUpController } from "../composables/useAdminStepUp"
defineProps<{ controller: AdminStepUpController }>()
</script>
<template>
  <el-dialog
    :model-value="controller.state.open"
    title="验证当前账号"
    width="420px"
    destroy-on-close
    :close-on-click-modal="false"
    @update:model-value="
      (open: boolean) => {
        if (!open) controller.cancel()
      }
    "
  >
    <p>{{ controller.state.label }}</p>
    <p>请输入当前登录认证源的密码。验证仅授权本次操作，五分钟内有效且只能使用一次。</p>
    <el-input
      :model-value="controller.state.password"
      type="password"
      autocomplete="current-password"
      placeholder="当前账号密码"
      :maxlength="128"
      :disabled="controller.state.busy"
      data-testid="admin-step-up-password"
      @update:model-value="controller.updatePassword"
      @keyup.enter="controller.submit"
    />
    <el-alert v-if="controller.state.error" :title="controller.state.error" type="error" :closable="false" />
    <template #footer>
      <el-button @click="controller.cancel">取消</el-button>
      <el-button
        type="primary"
        :loading="controller.state.busy"
        :disabled="!controller.state.password"
        data-testid="admin-step-up-submit"
        @click="controller.submit"
        >验证并继续</el-button
      >
    </template>
  </el-dialog>
</template>
