<script setup lang="ts">
import { ElMessage } from "element-plus"
import { ref, watch } from "vue"

import { addVendorTestRecipient, type VendorTestRecipient } from "../api/admin"
import { PHONE_RE } from "../lib/phone"

const props = defineProps<{ modelValue: boolean }>()

const emit = defineEmits<{
  "update:modelValue": [value: boolean]
  added: [value: VendorTestRecipient]
}>()

const label = ref("")
const phone = ref("")
const submitting = ref(false)

function clear(): void {
  label.value = ""
  phone.value = ""
}

function close(): void {
  clear()
  emit("update:modelValue", false)
}

async function submit(): Promise<void> {
  const normalizedLabel = label.value.trim()
  if (!normalizedLabel || !PHONE_RE.test(phone.value)) {
    ElMessage.warning("请填写用途标签和 11 位测试手机号")
    return
  }
  submitting.value = true
  try {
    const recipient = await addVendorTestRecipient(normalizedLabel, phone.value)
    emit("added", recipient)
    emit("update:modelValue", false)
    ElMessage.success("测试号码已加密登记")
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试号码登记失败")
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
    title="登记真实联调收件人"
    width="480px"
    destroy-on-close
    append-to-body
    class="vendor-recipient-dialog"
    @close="close"
    @closed="clear"
  >
    <div v-if="modelValue" class="vendor-sensitive-form">
      <el-alert
        title="一次只登记一个号码"
        description="手机号只在本次请求中传输，服务端立即生成 AES-GCM 密文、HMAC 索引和掩码；页面此后只显示掩码。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用途标签" required>
          <el-input v-model="label" data-testid="vendor-recipient-label" maxlength="64" placeholder="例如：值班机" />
        </el-form-item>
        <el-form-item label="测试手机号" required>
          <el-input
            v-model="phone"
            data-testid="vendor-recipient-phone"
            type="password"
            inputmode="numeric"
            autocomplete="off"
            spellcheck="false"
            maxlength="11"
            show-password
          />
        </el-form-item>
      </el-form>
    </div>
    <template #footer>
      <el-button :disabled="submitting" @click="close">取消登记</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">加密登记号码</el-button>
    </template>
  </el-dialog>
</template>
