<script setup lang="ts">
import { ref, watch, onScopeDispose } from "vue"
import { ElMessage } from "element-plus"
import {
  getSecurityDailyConfiguration,
  updateSecurityDailyConfiguration,
  type SecurityDailyConfiguration,
} from "../api/securityDaily"
import { useLatestRead } from "../composables/useLatestRead"
import { apiErrorMessage } from "../lib/securityDaily"
const configOpen = defineModel<boolean>({ default: false })
const emit = defineEmits<{ saved: []; loading: [value: boolean] }>()
const configLoading = ref(false)
const configSaving = ref(false)
const configErrorMessage = ref("")
const configEnabled = ref(false)
const configRecipients = ref("")
const configApiKey = ref("")
const clearConfigApiKey = ref(false)
const currentConfiguration = ref<SecurityDailyConfiguration | null>(null)
const read = useLatestRead()
watch(configLoading, (value) => emit("loading", value))

function clearConfigurationSecrets(): void {
  read.cancel()
  configLoading.value = false
  configApiKey.value = ""
  clearConfigApiKey.value = false
}

async function openConfiguration(): Promise<void> {
  const signal = read.start()
  configLoading.value = true
  configErrorMessage.value = ""
  configApiKey.value = ""
  clearConfigApiKey.value = false
  try {
    const configuration = await getSecurityDailyConfiguration()
    if (signal.aborted) return
    currentConfiguration.value = configuration
    configEnabled.value = configuration.enabled
    configRecipients.value = configuration.recipients.join("\n")
  } catch (error) {
    if (signal.aborted) return
    configErrorMessage.value = apiErrorMessage(error, "安全日报配置暂不可用，请刷新重试")
  } finally {
    if (!signal.aborted) configLoading.value = false
  }
}

function parseRecipients(): string[] {
  return configRecipients.value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const EMAIL_PATTERN =
  /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/

function validateRecipients(recipients: string[]): string {
  if (recipients.length > 3) return "收件人最多 3 个"
  const invalid = recipients.find(
    (item) => item.length > 254 || !EMAIL_PATTERN.test(item) || item.split("@")[0].length > 64,
  )
  if (invalid) return `收件人地址无效：${invalid}`
  const seen = new Set<string>()
  const duplicate = recipients.find((item) => {
    const key = item.toLowerCase()
    if (seen.has(key)) return true
    seen.add(key)
    return false
  })
  return duplicate ? `收件人不能重复：${duplicate}` : ""
}

async function saveConfiguration(): Promise<void> {
  const recipients = parseRecipients()
  const validationError = validateRecipients(recipients)
  if (validationError) {
    configErrorMessage.value = validationError
    return
  }
  configSaving.value = true
  configErrorMessage.value = ""
  try {
    currentConfiguration.value = await updateSecurityDailyConfiguration({
      enabled: configEnabled.value,
      recipients,
      resend_api_key: clearConfigApiKey.value ? "" : configApiKey.value.trim() || null,
    })
    configOpen.value = false
    ElMessage.success("安全日报配置已保存 · 本次操作已记入审计")
    emit("saved")
  } catch (error) {
    configErrorMessage.value = apiErrorMessage(error, "安全日报配置保存失败，请检查输入")
  } finally {
    configSaving.value = false
  }
}

watch(configOpen, (open) => {
  if (open) void openConfiguration()
  else clearConfigurationSecrets()
})
onScopeDispose(clearConfigurationSecrets)
</script>
<template>
  <el-dialog
    v-model="configOpen"
    title="安全日报邮件配置"
    width="560px"
    destroy-on-close
    @closed="clearConfigurationSecrets"
  >
    <el-skeleton v-if="configLoading" :rows="5" animated />
    <el-form v-else label-position="top" @submit.prevent="saveConfiguration">
      <el-form-item label="启用安全日报">
        <el-switch v-model="configEnabled" active-text="启用" inactive-text="停用" />
      </el-form-item>
      <el-form-item label="Resend API Key">
        <el-input
          v-model="configApiKey"
          type="password"
          show-password
          autocomplete="off"
          :disabled="clearConfigApiKey"
          placeholder="留空保持当前 Key"
        />
        <div class="form-tip"
          >当前状态：{{ currentConfiguration?.resend_api_key_configured ? "已配置" : "未配置" }}；Key 不会回显。</div
        >
        <el-checkbox v-if="currentConfiguration?.resend_api_key_configured" v-model="clearConfigApiKey"
          >清空当前 Key</el-checkbox
        >
      </el-form-item>
      <el-form-item label="收件人（每行一个，也可用逗号分隔，最多 3 个）">
        <el-input v-model="configRecipients" type="textarea" :rows="4" placeholder="security@example.com" />
      </el-form-item>
      <el-alert v-if="configErrorMessage" :title="configErrorMessage" type="error" show-icon :closable="false" />
    </el-form>
    <template #footer>
      <el-button @click="configOpen = false">取消</el-button>
      <el-button type="primary" :loading="configSaving" @click="saveConfiguration">保存</el-button>
    </template>
  </el-dialog>
</template>
