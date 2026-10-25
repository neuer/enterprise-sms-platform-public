<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import { exceedsCodePoints } from "../lib/codePoints"

const props = defineProps<{
  modelValue: string
  maxCodePoints: number
  multiline?: boolean
  rows?: number
  disabled?: boolean
  placeholder?: string
  ariaLabel?: string
  testId?: string
}>()

const emit = defineEmits<{
  "update:modelValue": [value: string]
}>()

const exceeded = ref(false)
const composing = ref(false)
const field = ref<{ textarea?: HTMLTextAreaElement; input?: HTMLInputElement } | null>(null)
let bound: HTMLInputElement | HTMLTextAreaElement | null = null

function nativeElement(): HTMLInputElement | HTMLTextAreaElement | null {
  return field.value?.textarea ?? field.value?.input ?? null
}

function tooLong(value: string): boolean {
  return exceedsCodePoints(value, props.maxCodePoints)
}

function onBeforeInput(event: Event): void {
  const input = event as InputEvent
  if (composing.value || input.isComposing) return
  const element = event.target
  if (!(element instanceof HTMLInputElement) && !(element instanceof HTMLTextAreaElement)) return
  const kind = input.inputType || ""
  if (kind.startsWith("delete") || kind === "historyUndo" || kind === "historyRedo") return
  if (!kind.startsWith("insert")) return
  const start = element.selectionStart ?? element.value.length
  const end = element.selectionEnd ?? element.value.length
  const next = element.value.slice(0, start) + (input.data ?? "") + element.value.slice(end)
  if (tooLong(next)) {
    input.preventDefault()
    exceeded.value = true
  }
}

function onPaste(event: Event): void {
  if (composing.value || !(event instanceof ClipboardEvent)) return
  const element = event.target
  if (!(element instanceof HTMLInputElement) && !(element instanceof HTMLTextAreaElement)) return
  const text = event.clipboardData?.getData("text") ?? ""
  const start = element.selectionStart ?? element.value.length
  const end = element.selectionEnd ?? element.value.length
  if (tooLong(element.value.slice(0, start) + text + element.value.slice(end))) {
    event.preventDefault()
    exceeded.value = true
  }
}

function onInput(value: string): void {
  if (composing.value) return
  if (tooLong(value)) {
    const element = nativeElement()
    if (element) element.value = props.modelValue
    exceeded.value = true
    return
  }
  exceeded.value = false
  emit("update:modelValue", value)
}

function onCompositionStart(): void {
  composing.value = true
}

function onCompositionEnd(event: Event): void {
  composing.value = false
  const element = event.target
  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) onInput(element.value)
}

function bind(element: HTMLInputElement | HTMLTextAreaElement): void {
  element.addEventListener("beforeinput", onBeforeInput)
  element.addEventListener("paste", onPaste)
  element.addEventListener("compositionstart", onCompositionStart)
  element.addEventListener("compositionend", onCompositionEnd)
}

function unbind(element: HTMLInputElement | HTMLTextAreaElement): void {
  element.removeEventListener("beforeinput", onBeforeInput)
  element.removeEventListener("paste", onPaste)
  element.removeEventListener("compositionstart", onCompositionStart)
  element.removeEventListener("compositionend", onCompositionEnd)
}

function rebind(): void {
  const next = nativeElement()
  if (bound === next) return
  if (bound) unbind(bound)
  bound = next
  if (bound) bind(bound)
}

onMounted(rebind)
watch(field, rebind)
onBeforeUnmount(() => {
  if (bound) unbind(bound)
})
</script>

<template>
  <div class="code-point-field">
    <el-input
      ref="field"
      :model-value="modelValue"
      :type="multiline ? 'textarea' : 'text'"
      :rows="rows"
      :disabled="disabled"
      :placeholder="placeholder"
      :aria-label="ariaLabel"
      :data-testid="testId"
      @update:model-value="onInput"
    />
    <p v-if="exceeded" class="code-point-exceeded" data-testid="code-point-exceeded" role="alert">
      已超过 {{ maxCodePoints }} 个字符，超限内容未写入。
    </p>
  </div>
</template>
