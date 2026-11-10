<script setup lang="ts">
/**
 * 登录输入框的显示/隐藏密码开关：只切换输入框的可见性，不读取输入值；每进入新的一步由页面复位为隐藏。
 * 指针按下不移走输入框焦点，点完可以接着输入；键盘操作时焦点留在开关上。
 */
const revealed = defineModel<boolean>({ required: true })
</script>

<template>
  <button
    class="login-peek"
    data-testid="login-peek"
    type="button"
    :aria-pressed="revealed"
    :aria-label="revealed ? '隐藏密码' : '显示密码'"
    @mousedown.prevent
    @click="revealed = !revealed"
  >
    {{ revealed ? "隐藏" : "显示" }}
  </button>
</template>

<style scoped>
@layer components {
  .login-peek {
    flex: none;
    min-height: 36px;
    padding: 0 8px;
    color: var(--login-chip);
    font: inherit;
    font-size: var(--fs-md);
    cursor: pointer;
    background: transparent;
    border: 0;
    border-radius: 8px;
  }
}
</style>
