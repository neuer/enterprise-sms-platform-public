<script setup lang="ts">
import { ElMessage } from "element-plus"
import { reactive, ref, watch } from "vue"

import {
  createVendorSealSession,
  installVendorCredentials,
  issueVendorTestStepUp,
  type VendorTestOperation,
} from "../api/admin"
import {
  clearCredentialDraft,
  sealVendorCredentials,
  type VendorCredentialDraft,
} from "../lib/vendorSeal"

const props = defineProps<{
  modelValue: boolean
  operation: "install_credentials" | "rotate_credentials"
}>()

const emit = defineEmits<{
  "update:modelValue": [value: boolean]
  operation: [value: VendorTestOperation]
}>()

const submitting = ref(false)
const password = ref("")
const draft = reactive<VendorCredentialDraft>({ secretName: "", secretKey: "" })

const title = () => (props.operation === "rotate_credentials" ? "轮换正式凭据" : "安装正式凭据")

function clear(): void {
  password.value = ""
  clearCredentialDraft(draft)
}

function close(): void {
  clear()
  emit("update:modelValue", false)
}

async function submit(): Promise<void> {
  if (!password.value || !draft.secretName.trim() || !draft.secretKey.trim()) {
    ElMessage.warning("请填写当前账号密码、SecretName 和 SecretKey")
    return
  }
  submitting.value = true
  try {
    const stepUp = await issueVendorTestStepUp(props.operation, password.value)
    const session = await createVendorSealSession(props.operation)
    const envelope = await sealVendorCredentials(session, draft)
    const operation = await installVendorCredentials(props.operation, stepUp.token, envelope)
    emit("operation", operation)
    ElMessage.success("正式凭据操作已进入受控执行队列")
    emit("update:modelValue", false)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "正式凭据提交失败")
  } finally {
    clear()
    submitting.value = false
  }
}

watch(
  () => props.modelValue,
  (visible) => {
    if (!visible) clear()
  },
)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title()"
    width="520px"
    destroy-on-close
    append-to-body
    class="vendor-credential-dialog"
    @close="close"
    @closed="clear"
  >
    <div v-if="modelValue" class="vendor-sensitive-form">
      <el-alert
        title="浏览器仅在内存中完成混合加密"
        description="SecretName 与 SecretKey 不会进入系统配置、浏览器存储、日志或 API 明文请求；提交后立即清空。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="当前账号密码" required>
          <el-input
            v-model="password"
            data-testid="vendor-credential-password"
            type="password"
            autocomplete="current-password"
            spellcheck="false"
            show-password
          />
        </el-form-item>
        <el-form-item label="运营商 SecretName" required>
          <el-input
            v-model="draft.secretName"
            data-testid="vendor-secret-name"
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            show-password
          />
        </el-form-item>
        <el-form-item label="运营商 SecretKey" required>
          <el-input
            v-model="draft.secretKey"
            data-testid="vendor-secret-key"
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            show-password
          />
        </el-form-item>
      </el-form>
      <p class="vendor-sensitive-note">页面不会校验或展示凭据值；安装结果只显示成功、失败与安全错误代码。</p>
    </div>
    <template #footer>
      <el-button :disabled="submitting" @click="close">保留现状</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">
        {{ operation === 'rotate_credentials' ? '密封并轮换' : '密封并安装' }}
      </el-button>
    </template>
  </el-dialog>
</template>
