<script setup lang="ts">
import { ElMessage } from "element-plus"
import { onBeforeUnmount, ref, watch } from "vue"

import PhoneMask from "./PhoneMask.vue"
import { defaultSessionDocument } from "../api/sessionDocument"
import { SESSION_CLEARING_EVENT } from "../api/sessionEvents"
import { errorText } from "../lib/error"

/** 授权查看手机号：默认掩码 + 「授权查看」，解密成功后内联展示明文；明文只存组件内存（可「重新隐藏」清空，卸载时自动清空），不持久化。 */
const props = withDefaults(
  defineProps<{
    /** 掩码号码（phone_mask），解密前的展示值。 */
    masked: string
    /** 父级受控解密调用（服务端记敏感读审计），resolve 明文、reject 即失败。 */
    reveal: () => Promise<string>
    /** 展示所属记录的稳定身份；掩码和渲染时创建的回调不能用作身份。 */
    recordKey?: string | number
    /** 保留挂载的抽屉在关闭起点即设为 false，不等待关闭动画结束。 */
    active?: boolean
    /** 透传到「授权查看」按钮的 data-testid。 */
    testid?: string
  }>(),
  { active: true },
)

const emit = defineEmits<{
  /** 解密成功后上抛明文，供父级联动徽标等易失展示；父级同样不得持久化。 */
  revealed: [phone: string]
}>()

const revealing = ref(false)
const revealedPhone = ref("")
let generation = 0
let disposed = false

/** 先废弃在途结果再清空展示；关闭后重新打开也不能接纳上一代响应。 */
function invalidate(): void {
  generation += 1
  revealedPhone.value = ""
  revealing.value = false
}

async function onReveal(): Promise<void> {
  if (disposed || !props.active || revealing.value || revealedPhone.value) return
  const current = ++generation
  const recordKey = props.recordKey
  const origin = defaultSessionDocument.captureOrigin()
  const isCurrent = () =>
    !disposed &&
    props.active &&
    current === generation &&
    props.recordKey === recordKey &&
    defaultSessionDocument.isOriginCurrent(origin)
  revealing.value = true
  try {
    const phone = await props.reveal()
    if (!isCurrent()) return
    revealedPhone.value = phone
    emit("revealed", revealedPhone.value)
    if (isCurrent()) ElMessage.success("已解密 · 本次授权查看已记入审计")
  } catch (error) {
    if (isCurrent()) ElMessage.error(errorText(error, "解密失败"))
  } finally {
    if (isCurrent()) revealing.value = false
  }
}

/** 清空内存明文回到掩码态；不解密、不再记审计（审计只发生在 reveal 侧）。 */
function onHide(): void {
  invalidate()
}

watch(() => [props.active, props.recordKey], invalidate, { flush: "sync" })
window.addEventListener(SESSION_CLEARING_EVENT, invalidate)
onBeforeUnmount(() => {
  disposed = true
  invalidate()
  window.removeEventListener(SESSION_CLEARING_EVENT, invalidate)
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
      <el-button link type="primary" :disabled="!active" :loading="revealing" :data-testid="testid" @click="onReveal"
        >授权查看</el-button
      >
    </template>
  </span>
</template>
