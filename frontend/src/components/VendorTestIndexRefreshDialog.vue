<script setup lang="ts">
import { ElMessage } from "element-plus"
import { ref } from "vue"

import { refreshVendorTestRecipientIndex, type VendorTestRecipient } from "../api/admin"
import PhoneMask from "./PhoneMask.vue"
import { errorText } from "../lib/error"
import { PHONE_RE } from "../lib/phone"

/**
 * 测试号码索引刷新对话框：重新输入的手机号只在本组件内存中用于重建跨版本 HMAC 索引，
 * 提交结束与关闭时立即清空，不解密或回显历史号码。
 */
const emit = defineEmits<{ refreshed: [recipient: VendorTestRecipient] }>()

const refreshVisible = ref(false)
const refreshRecipient = ref<VendorTestRecipient | null>(null)
const refreshPhone = ref("")
const refreshBusy = ref(false)

function open(recipient: VendorTestRecipient): void {
  refreshRecipient.value = recipient
  refreshPhone.value = ""
  refreshVisible.value = true
}

function clearIndexRefresh(): void {
  refreshPhone.value = ""
  refreshRecipient.value = null
}

async function submitIndexRefresh(): Promise<void> {
  const recipient = refreshRecipient.value
  const phone = refreshPhone.value
  if (!recipient || !PHONE_RE.test(phone)) {
    ElMessage.warning("请输入 11 位测试手机号")
    return
  }
  refreshBusy.value = true
  try {
    const refreshed = await refreshVendorTestRecipientIndex(recipient.id, phone)
    emit("refreshed", refreshed)
    refreshVisible.value = false
    ElMessage.success("号码索引已覆盖当前全部密钥版本")
  } catch (error) {
    ElMessage.error(errorText(error, "号码索引刷新失败"))
  } finally {
    refreshPhone.value = ""
    refreshBusy.value = false
  }
}

defineExpose({ open })
</script>

<template>
  <el-dialog
    v-model="refreshVisible"
    title="刷新号码索引"
    width="min(440px, 92vw)"
    destroy-on-close
    append-to-body
    @closed="clearIndexRefresh"
  >
    <div v-if="refreshVisible" class="vendor-sensitive-form">
      <p>
        数据密钥轮换后，请重新输入
        <PhoneMask v-if="refreshRecipient" :value="refreshRecipient.phone_mask" /> 对应的同一号码。 系统只重建跨版本
        HMAC 索引，不解密或回显历史号码。
      </p>
      <el-input
        v-model="refreshPhone"
        data-testid="vendor-refresh-phone"
        inputmode="numeric"
        maxlength="11"
        autocomplete="off"
        spellcheck="false"
        placeholder="请输入同一测试手机号"
        @keyup.enter="submitIndexRefresh"
      />
    </div>
    <template #footer>
      <el-button :disabled="refreshBusy" @click="refreshVisible = false">保留现状</el-button>
      <el-button data-testid="vendor-refresh-submit" type="primary" :loading="refreshBusy" @click="submitIndexRefresh"
        >确认刷新</el-button
      >
    </template>
  </el-dialog>
</template>
