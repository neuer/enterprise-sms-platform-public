<script setup lang="ts">
import { ElMessage } from "element-plus"
import { computed, onBeforeUnmount, ref } from "vue"

import {
  activateVendorTest,
  issueVendorTestStepUp,
  resetVendorTest,
  resumeVendorTest,
  type VendorTestOperation,
} from "../api/admin"
import { errorText } from "../lib/error"

type StepUpAction = "activate" | "reset_configuration" | "resume_critical"

/**
 * 真实联调二次认证对话框：当前账号密码与切回确认短语只存在于本组件，
 * 关闭动画起点、提交结束与卸载时立即清空；认证令牌单次使用，不写入浏览器存储。
 * busy 与联调控制台共享，提交在途期间禁止关闭与重复提交。
 */
const busy = defineModel<boolean>("busy", { default: false })
const emit = defineEmits<{ operation: [value: VendorTestOperation] }>()
// v-model 回写要等父组件重渲染才生效，同步连点须由本地标志即时拦截。
const submitting = ref(false)
const locked = computed(() => busy.value || submitting.value)

const RESET_CONFIRMATION = "切回Mock"
const stepUpVisible = ref(false)
const stepUpPassword = ref("")
const resetConfirmation = ref("")
const stepUpAction = ref<StepUpAction | null>(null)

function clearStepUpSecrets(): void {
  stepUpPassword.value = ""
  resetConfirmation.value = ""
}

function clearStepUp(): void {
  clearStepUpSecrets()
  stepUpAction.value = null
}

function closeStepUp(): void {
  clearStepUpSecrets()
  stepUpVisible.value = false
}

function open(action: StepUpAction): void {
  clearStepUpSecrets()
  stepUpAction.value = action
  stepUpVisible.value = true
}

async function submitStepUp(): Promise<void> {
  if (locked.value) return
  const action = stepUpAction.value
  if (!action || !stepUpPassword.value) {
    ElMessage.warning("请输入当前账号密码")
    return
  }
  if (action === "reset_configuration" && resetConfirmation.value !== RESET_CONFIRMATION) {
    ElMessage.warning(`请输入精确短语“${RESET_CONFIRMATION}”`)
    clearStepUpSecrets()
    return
  }
  submitting.value = true
  busy.value = true
  try {
    const token = await issueVendorTestStepUp(action, stepUpPassword.value)
    const operation =
      action === "activate"
        ? await activateVendorTest(token.token)
        : action === "reset_configuration"
          ? await resetVendorTest(token.token)
          : await resumeVendorTest(token.token)
    stepUpVisible.value = false
    emit("operation", operation)
  } catch (error) {
    ElMessage.error(errorText(error, "二次认证操作失败"))
  } finally {
    clearStepUpSecrets()
    submitting.value = false
    busy.value = false
  }
}

onBeforeUnmount(() => {
  clearStepUp()
})

defineExpose({ open })
</script>

<template>
  <el-dialog
    v-model="stepUpVisible"
    :title="
      stepUpAction === 'activate'
        ? '二次认证激活'
        : stepUpAction === 'reset_configuration'
          ? '切回 Mock'
          : '二次认证恢复'
    "
    width="440px"
    destroy-on-close
    append-to-body
    class="vendor-step-up-dialog"
    :close-on-click-modal="!locked"
    :close-on-press-escape="!locked"
    :show-close="!locked"
    @close="clearStepUpSecrets"
    @closed="clearStepUp"
  >
    <div v-if="stepUpVisible" class="vendor-sensitive-form">
      <el-alert
        v-if="stepUpAction === 'reset_configuration'"
        id="vendor-reset-consequences"
        title="仅影响测试环境的厂商连接，操作不可撤销"
        type="error"
        :closable="false"
        show-icon
      >
        <p>
          测试环境将停止真实发送与厂商状态/回复拉取，切回本机 Mock，并删除测试环境的正式厂商凭据
          全部版本。生产环境的配置、凭据、服务和数据不受影响。
        </p>
        <p>
          保留全部加密测试号码及其索引，也保留管理员、短信业务数据、审计记录、当日 UAT 用量、 uncertain
          占额、数据库、Docker volume 和运行态目录；切换前已发送、待回执、uncertain
          或被错误环境消费的历史状态不会自动修复。这不是系统初始化。
        </p>
      </el-alert>
      <p>请输入当前登录账号密码。认证令牌五分钟内单次有效，不写入浏览器存储。</p>
      <el-form label-position="top" @submit.prevent="submitStepUp">
        <el-form-item label="当前 Provider 密码" required>
          <el-input
            v-model="stepUpPassword"
            :data-testid="stepUpAction === 'reset_configuration' ? 'vendor-reset-password' : 'vendor-step-up-password'"
            type="password"
            autocomplete="current-password"
            spellcheck="false"
            show-password
          />
        </el-form-item>
        <el-form-item v-if="stepUpAction === 'reset_configuration'" :label="`输入“${RESET_CONFIRMATION}”确认`" required>
          <el-input
            v-model="resetConfirmation"
            data-testid="vendor-reset-confirmation"
            autocomplete="off"
            spellcheck="false"
            aria-describedby="vendor-reset-consequences"
          />
        </el-form-item>
      </el-form>
    </div>
    <template #footer>
      <el-button
        :data-testid="stepUpAction === 'reset_configuration' ? 'vendor-reset-cancel' : undefined"
        :disabled="locked"
        @click="closeStepUp"
        >{{ stepUpAction === "reset_configuration" ? "保留现状" : "继续检查" }}</el-button
      >
      <el-button
        :data-testid="stepUpAction === 'reset_configuration' ? 'vendor-reset-submit' : undefined"
        :type="stepUpAction === 'reset_configuration' ? 'danger' : 'primary'"
        :loading="locked"
        @click="submitStepUp"
      >
        {{
          stepUpAction === "activate"
            ? "验证并激活"
            : stepUpAction === "reset_configuration"
              ? "验证并切回 Mock"
              : "验证并恢复"
        }}
      </el-button>
    </template>
  </el-dialog>
</template>

<!--
  本样式块刻意非 scoped：el-dialog 经 append-to-body 挂载到 <body>，
  不在本组件的 scoped DOM 子树内，scoped 选择器无法命中。类名以 vendor- 前缀隔离。
-->
<style>
@layer components {
  .vendor-step-up-dialog {
    max-width: calc(100vw - 32px);
  }

  .vendor-step-up-dialog .el-input__wrapper,
  .vendor-step-up-dialog .el-dialog__footer .el-button {
    min-height: 44px;
  }

  .vendor-step-up-dialog .el-dialog__footer {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: flex-end;
  }

  .vendor-step-up-dialog .el-dialog__footer .el-button + .el-button {
    margin-left: 0;
  }

  @media (max-width: 360px) {
    .vendor-step-up-dialog .el-dialog__footer .el-button {
      flex: 1 1 100%;
      margin-left: 0;
    }
  }
}
</style>
