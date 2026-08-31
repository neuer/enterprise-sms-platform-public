<script setup lang="ts">
import { ElMessage } from "element-plus"
import { ref } from "vue"

import PhoneMask from "./PhoneMask.vue"

/** 授权查看手机号：默认掩码 + 「授权查看」，解密成功后内联展示明文；明文只存组件内存，不持久化。 */
const props = defineProps<{
  /** 掩码号码（phone_mask），解密前的展示值。 */
  masked: string
  /** 父级受控解密调用（服务端记敏感读审计），resolve 明文、reject 即失败。 */
  reveal: () => Promise<string>
  /** 透传到「授权查看」按钮的 data-testid。 */
  testid?: string
}>()

const emit = defineEmits<{
  /** 解密成功后上抛明文，供父级联动徽标等易失展示；父级同样不得持久化。 */
  revealed: [phone: string]
}>()

const revealing = ref(false)
const revealedPhone = ref("")

async function onReveal(): Promise<void> {
  if (revealing.value || revealedPhone.value) return
  revealing.value = true
  try {
    revealedPhone.value = await props.reveal()
    emit("revealed", revealedPhone.value)
    ElMessage.success("已解密 · 本次授权查看已记入审计")
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "解密失败")
  } finally {
    revealing.value = false
  }
}
</script>

<template>
  <span class="phone-reveal">
    <strong v-if="revealedPhone" class="revealed-phone">{{ revealedPhone }}</strong>
    <template v-else>
      <PhoneMask :value="masked" />
      <el-button link type="primary" :loading="revealing" :data-testid="testid" @click="onReveal">授权查看</el-button>
    </template>
  </span>
</template>
