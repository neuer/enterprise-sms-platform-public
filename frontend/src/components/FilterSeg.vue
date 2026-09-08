<script setup lang="ts" generic="V">
/**
 * 分段筛选控件单点：role="group" + 选项按钮组。点同值不重复 emit；
 * 点选后是否重查由父级 handler 决定（点选即重查的页面把既有 setter 接到 update:modelValue 上）。
 * 计数徽标等按钮附加内容经 #option 插槽渲染；样式变体经 class（filter-seg--compact/--pill/--chips）承载。
 */
export interface FilterSegOption<V> {
  label: string
  value: V
  /** 按钮 :key 与 testid 后缀；缺省取 String(value)。 */
  key?: string | number
  /** 覆盖按钮 testid（不规则命名，如 message-view-list）。 */
  testid?: string
  disabled?: boolean
  /** 追加到按钮的类名（如批次状态分组的 hot）。 */
  class?: string
  /** 计数徽标等附加数据，经 #option 插槽由父级渲染。 */
  count?: number | null
}

const props = withDefaults(
  defineProps<{
    modelValue: V
    options: FilterSegOption<V>[]
    /** 按钮 testid 前缀：按钮 testid 为 `${前缀}-${key ?? String(value)}`；不传则按钮不带 testid。 */
    buttonTestidPrefix?: string
    disabled?: boolean
  }>(),
  { buttonTestidPrefix: undefined, disabled: false },
)

const emit = defineEmits<{ "update:modelValue": [value: V] }>()

function optionKey(option: FilterSegOption<V>): string | number {
  return option.key ?? String(option.value)
}

function optionTestid(option: FilterSegOption<V>): string | undefined {
  if (option.testid) return option.testid
  return props.buttonTestidPrefix ? `${props.buttonTestidPrefix}-${optionKey(option)}` : undefined
}

function pick(option: FilterSegOption<V>): void {
  if (props.disabled || option.disabled) return
  if (option.value === props.modelValue) return
  emit("update:modelValue", option.value)
}
</script>

<template>
  <div class="filter-seg" role="group">
    <slot name="prefix" />
    <button
      v-for="option in options"
      :key="optionKey(option)"
      type="button"
      :class="[{ on: modelValue === option.value }, option.class]"
      :data-testid="optionTestid(option)"
      :aria-pressed="modelValue === option.value"
      :disabled="disabled || option.disabled || undefined"
      @click="pick(option)"
      ><slot name="option" :option="option" :on="modelValue === option.value">{{ option.label }}</slot></button
    >
    <slot name="suffix" />
  </div>
</template>
