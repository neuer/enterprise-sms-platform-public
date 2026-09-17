<script setup lang="ts">
import { ElMessage } from "element-plus"
import { onBeforeUnmount, ref } from "vue"

import PhoneMask from "./PhoneMask.vue"
import { errorText } from "../lib/error"

/** 授权查看手机号：默认掩码 + 「授权查看」，解密成功后内联展示明文；明文只存组件内存（可「重新隐藏」清空，卸载时自动清空），不持久化。 */
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
    ElMessage.error(errorText(error, "解密失败"))
  } finally {
    revealing.value = false
  }
}

/** 清空内存明文回到掩码态；不解密、不再记审计（审计只发生在 reveal 侧）。 */
function onHide(): void {
  revealedPhone.value = ""
}

// 组件卸载前显式清空明文引用，避免明文滞留于已卸载组件的内存快照。
onBeforeUnmount(() => {
  revealedPhone.value = ""
})
</script>

<template>
  <span class="phone-reveal">
    <template v-if="revealedPhone">
      <strong class="revealed-phone">{{ revealedPhone }}</strong>
      <el-button link type="primary" data-testid="phone-hide" @click="onHide">重新隐藏</el-button>
    </template>
    <template v-else>
      <PhoneMask :value="masked" />
      <el-button link type="primary" :loading="revealing" :data-testid="testid" @click="onReveal">授权查看</el-button>
    </template>
  </span>
</template>
