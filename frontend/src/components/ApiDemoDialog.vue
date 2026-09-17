<script setup lang="ts">
import { useApprovedResources } from "../composables/useApprovedResources"
import { computed, ref, toRef, watch } from "vue"
import { ElMessage } from "element-plus"
import type { ManagedApp } from "../api/apps"
import { listTemplates } from "../api/templates"
import { copyText } from "../lib/clipboard"
import { errorText } from "../lib/error"
import {
  DEMO_LANGUAGES,
  DEMO_LABELS,
  buildDemoScript,
  exampleParamsFor,
  type DemoLanguage,
  type DemoContext,
} from "../lib/demoScript"
const demoOpen = defineModel<boolean>({ default: false })
const props = defineProps<{ app: ManagedApp | null }>()
const demoApp = toRef(props, "app")
const demoLang = ref<DemoLanguage>("curl")
const {
  approved: approvedTemplates,
  loading: demoTemplatesLoading,
  load: loadTemplates,
} = useApprovedResources(listTemplates, (error) => ElMessage.error(errorText(error, "模板加载失败")))
const demoTemplateId = ref<number | null>(null)
const demoTemplate = computed(() => approvedTemplates.value.find((item) => item.id === demoTemplateId.value) ?? null)
const demoParamsSummary = computed(() => {
  const template = demoTemplate.value
  if (!template || !template.var_specs.length) return "无变量"
  return [...template.var_specs]
    .sort((a, b) => a.pos - b.pos)
    .map((spec) => `{${spec.pos}}≤${spec.max_len}`)
    .join("，")
})
const demoContext = computed<DemoContext | null>(() => {
  const app = demoApp.value
  const template = demoTemplate.value
  if (!app || !template) return null
  return {
    app,
    templateId: template.id,
    templateName: template.name,
    templateContent: template.content,
    params: exampleParamsFor(template.var_specs),
  }
})
const demoScript = computed(() => {
  const app = demoApp.value
  if (!app) return ""
  const context = demoContext.value ?? {
    app,
    templateId: 12,
    templateName: "（请选择已审核模板）",
    templateContent: "",
    params: ["张三", "123456"],
  }
  return buildDemoScript(demoLang.value, context)
})

async function loadApprovedTemplates(): Promise<void> {
  await loadTemplates()
  if (demoTemplateId.value === null && approvedTemplates.value.length)
    demoTemplateId.value = approvedTemplates.value[0].id
}

watch(demoOpen, (open) => {
  if (!open) return
  demoLang.value = "curl"
  demoTemplateId.value = null
  void loadApprovedTemplates()
})

async function copyDemo(): Promise<void> {
  if (!demoScript.value) return
  if (await copyText(demoScript.value)) {
    ElMessage.success("脚本已复制到剪贴板")
  } else {
    ElMessage.error("复制失败，请手动选择文本复制")
  }
}
</script>
<template>
  <el-dialog
    v-model="demoOpen"
    :title="demoApp ? `接入示例 · ${demoApp.name}` : '接入示例'"
    width="min(720px, 96vw)"
    :close-on-click-modal="false"
    class="demo-dialog"
  >
    <p class="muted"
      >应用 #{{ demoApp?.id }} · {{ demoApp?.dept }} · 类别 {{ (demoApp?.allowed_categories || []).join(" / ") }}</p
    >
    <p
      >正式接入必须使用已审核模板（template_id）发送；直接内容会进入服务商人工审核、发送延迟大。API Key
      请通过环境变量注入，不要硬编码或写入日志。</p
    >
    <label class="muted" for="demo-template-select">已审核模板</label>
    <el-select
      v-model="demoTemplateId"
      data-testid="demo-template-select"
      placeholder="选择已审核模板"
      :loading="demoTemplatesLoading"
      style="width: 100%"
    >
      <el-option
        v-for="template in approvedTemplates"
        :key="template.id"
        :value="template.id"
        :label="'#' + template.id + ' · ' + template.name"
      />
    </el-select>
    <p v-if="demoTemplate" data-testid="demo-template-info">
      模板内容：{{ demoTemplate.content }} · 参数：{{ demoParamsSummary }}
    </p>
    <p v-else>暂无已审核模板，示例将使用占位模板 ID；请先在「模板管理」创建模板并提交审核。</p>
    <el-tabs v-model="demoLang">
      <el-tab-pane v-for="language in DEMO_LANGUAGES" :key="language" :label="DEMO_LABELS[language]" :name="language">
        <pre class="demo-script" :data-testid="`demo-script-body-${language}`">{{ demoScript }}</pre>
      </el-tab-pane>
    </el-tabs>
    <template #footer>
      <el-button data-testid="demo-copy" :disabled="!demoScript" @click="copyDemo">复制脚本</el-button>
      <el-button data-testid="demo-close" type="primary" @click="demoOpen = false">关闭</el-button>
    </template>
  </el-dialog>
</template>
