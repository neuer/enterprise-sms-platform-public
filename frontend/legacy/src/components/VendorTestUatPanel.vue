<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref, watch } from "vue"

import {
  previewVendorTestUat,
  sendVendorTestUat,
  type VendorTestOperation,
  type VendorTestRecipient,
} from "../api/admin"
import type { ManagedApp } from "../api/apps"
import { listSigns, type SmsSign } from "../api/signs"
import { listTemplates, type SmsTemplate } from "../api/templates"
import type { BillingPreview } from "../api/webMessages"

const props = defineProps<{
  disabled: boolean
  recipients: VendorTestRecipient[]
  apps: ManagedApp[]
  dailyLimit: number
}>()

const emit = defineEmits<{ operation: [value: VendorTestOperation] }>()

type UatCategory = "verify" | "notice" | "market"
type UatContentMode = "content" | "template"

const recipientId = ref<number | null>(null)
const appId = ref<number | null>(null)
const category = ref<UatCategory>("notice")
const contentMode = ref<UatContentMode>("content")
const content = ref("")
const templates = ref<SmsTemplate[]>([])
const templateId = ref<number | null>(null)
const templateParams = ref<string[]>([])
const signs = ref<SmsSign[]>([])
const signName = ref("")
const consentConfirmed = ref(false)
const preview = ref<BillingPreview | null>(null)
const previewing = ref(false)
const sending = ref(false)

const selectedApp = computed(() => props.apps.find((item) => item.id === appId.value) || null)
const categories = computed<UatCategory[]>(() => {
  const allowed = selectedApp.value?.allowed_categories || ["verify", "notice", "market"]
  return allowed.filter((item): item is UatCategory => ["verify", "notice", "market"].includes(item))
})
const selectedRecipient = computed(
  () => props.recipients.find((item) => item.id === recipientId.value) || null,
)
const approvedTemplates = computed(() =>
  templates.value.filter((item) => item.vendor_state === "approved"),
)
const approvedSigns = computed(() => signs.value.filter((item) => item.vendor_state === "approved"))
const selectedTemplate = computed(
  () => approvedTemplates.value.find((item) => item.id === templateId.value) || null,
)
const renderedTemplate = computed(() => {
  const template = selectedTemplate.value
  if (!template) return ""
  return template.content.replace(/\{(\d+)\}/g, (placeholder, position: string) => {
    const value = templateParams.value[Number(position) - 1]?.trim()
    return value || placeholder
  })
})
const messageReady = computed(() => {
  if (contentMode.value === "content") return content.value.trim().length > 0
  const specs = selectedTemplate.value?.var_specs
  if (!specs || specs.length !== templateParams.value.length) return false
  return specs.every((spec) => {
    const value = templateParams.value[spec.pos - 1]?.trim() || ""
    return value.length > 0 && value.length <= spec.max_len
  })
})
const formReady = computed(
  () =>
    !props.disabled &&
    recipientId.value !== null &&
    appId.value !== null &&
    messageReady.value &&
    (category.value !== "market" || consentConfirmed.value),
)

watch(
  [appId, category, contentMode, content, templateId, templateParams, signName, consentConfirmed],
  () => {
    preview.value = null
    if (!categories.value.includes(category.value)) {
      category.value = categories.value[0] || "notice"
    }
  },
  { deep: true },
)

function selectTemplate(value: number | string): void {
  templateId.value = Number(value)
  const template = approvedTemplates.value.find((item) => item.id === Number(value))
  templateParams.value = template?.var_specs.map(() => "") || []
}

function messagePayload():
  | { content: string }
  | { template_id: number; template_params: string[] } {
  if (contentMode.value === "content") return { content: content.value.trim() }
  return {
    template_id: templateId.value!,
    template_params: templateParams.value.map((value) => value.trim()),
  }
}

async function loadApprovedOptions(): Promise<void> {
  const [templateResult, signResult] = await Promise.allSettled([listTemplates(), listSigns()])
  if (templateResult.status === "fulfilled") templates.value = templateResult.value
  else ElMessage.error("已审核模板加载失败")
  if (signResult.status === "fulfilled") signs.value = signResult.value
  else ElMessage.error("已审核签名加载失败")
}

async function runPreview(): Promise<boolean> {
  if (!formReady.value) {
    ElMessage.warning("请先选择收件人、应用、类别并填写完整内容")
    return false
  }
  previewing.value = true
  try {
    preview.value = await previewVendorTestUat({
      app_id: appId.value!,
      category: category.value,
      ...messagePayload(),
      sign_name: signName.value || undefined,
      consent_confirmed: consentConfirmed.value,
    })
    return true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "计费预览失败")
    return false
  } finally {
    previewing.value = false
  }
}

async function send(): Promise<void> {
  if (!formReady.value || !selectedRecipient.value) {
    ElMessage.warning("真实 UAT 信息尚未填写完整")
    return
  }
  if (!preview.value && !(await runPreview())) return
  const billing = preview.value
  if (!billing) return
  try {
    await ElMessageBox.confirm(
      `将向 ${selectedRecipient.value.label}（${selectedRecipient.value.phone_mask}）发送 1 个真实号码。本次预计消耗 ${billing.quota_cost} 条计费额度（${billing.est_segments} 个计费段）；受控联调每日总上限为 ${props.dailyLimit} 条。`,
      "确认发送真实 UAT",
      {
        type: "warning",
        confirmButtonText: `确认发送（预计 ${billing.quota_cost} 条）`,
        cancelButtonText: "继续检查",
      },
    )
    sending.value = true
    const operation = await sendVendorTestUat({
      recipient_id: recipientId.value!,
      app_id: appId.value!,
      category: category.value,
      ...messagePayload(),
      sign_name: signName.value || undefined,
      consent_confirmed: consentConfirmed.value,
      remark: "系统配置页真实 UAT",
    })
    emit("operation", operation)
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "真实 UAT 提交失败")
    }
  } finally {
    sending.value = false
  }
}

onMounted(() => void loadApprovedOptions())
</script>

<template>
  <section class="vendor-uat-panel" aria-labelledby="vendor-uat-title">
    <header>
      <div>
        <p class="eyebrow">SINGLE RECIPIENT UAT</p>
        <h3 id="vendor-uat-title">单号码真实发送</h3>
      </div>
      <span>每日总预算 {{ dailyLimit }} 条</span>
    </header>

    <el-alert
      v-if="disabled"
      title="当前不可发送"
      description="仅在受控联调已激活、正式凭据与测试号码均就绪时开放。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-form class="vendor-uat-form" label-position="top" @submit.prevent="send">
      <div class="vendor-uat-grid">
        <el-form-item label="登记收件人" required>
          <el-select v-model="recipientId" data-testid="uat-recipient" placeholder="选择掩码号码" :disabled="disabled">
            <el-option
              v-for="recipient in recipients"
              :key="recipient.id"
              :label="`${recipient.label} · ${recipient.phone_mask}`"
              :value="recipient.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="应用" required>
          <el-select v-model="appId" data-testid="uat-app" placeholder="选择启用应用" :disabled="disabled">
            <el-option v-for="app in apps" :key="app.id" :label="`${app.name} · ${app.dept}`" :value="app.id" />
          </el-select>
        </el-form-item>
      </div>
      <el-form-item label="消息类别" required>
        <el-segmented
          v-model="category"
          :options="categories.map((value) => ({ label: { verify: '验证码', notice: '通知', market: '营销' }[value], value }))"
          :disabled="disabled"
        />
      </el-form-item>
      <el-form-item label="内容方式" required>
        <el-segmented
          v-model="contentMode"
          data-testid="uat-content-mode"
          :options="[
            { label: '直接编辑', value: 'content' },
            { label: '审核模板', value: 'template' },
          ]"
          :disabled="disabled"
        />
      </el-form-item>
      <el-form-item v-if="contentMode === 'content'" label="短信内容" required>
        <el-input
          v-model="content"
          data-testid="uat-content"
          type="textarea"
          :rows="4"
          maxlength="500"
          show-word-limit
          :disabled="disabled"
          placeholder="输入本次真实联调内容"
        />
      </el-form-item>
      <div v-else class="vendor-uat-template-fields">
        <el-form-item label="已审核模板" required>
          <el-select
            v-model="templateId"
            data-testid="uat-template"
            filterable
            placeholder="选择厂商已审核模板"
            :disabled="disabled"
            @change="selectTemplate"
          >
            <el-option
              v-for="item in approvedTemplates"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <template v-if="selectedTemplate">
          <el-form-item
            v-for="spec in selectedTemplate.var_specs"
            :key="spec.pos"
            :label="`模板参数 {${spec.pos}}`"
            required
          >
            <el-input
              v-model="templateParams[spec.pos - 1]"
              :data-testid="`uat-template-param-${spec.pos}`"
              :maxlength="spec.max_len"
              show-word-limit
              :disabled="disabled"
              :placeholder="`最多 ${spec.max_len} 字`"
            />
          </el-form-item>
          <div class="template-render-preview">
            <span>本地只读预览，服务端仍会重新校验</span>
            <p>{{ renderedTemplate }}</p>
          </div>
        </template>
        <el-alert
          v-else-if="!approvedTemplates.length"
          title="当前没有已审核模板"
          description="请先在模板管理中完成厂商审核。"
          type="info"
          :closable="false"
        />
      </div>
      <el-form-item label="发送签名">
        <el-select
          v-model="signName"
          data-testid="uat-sign"
          clearable
          filterable
          placeholder="留空则使用应用默认签名"
          :disabled="disabled"
        >
          <el-option
            v-for="item in approvedSigns"
            :key="item.id"
            :label="item.name"
            :value="item.name"
          />
        </el-select>
        <small class="vendor-uat-sign-hint">
          {{ selectedApp?.default_sign ? `应用默认：${selectedApp.default_sign}` : "应用未配置默认签名" }}
        </small>
      </el-form-item>
      <el-checkbox v-if="category === 'market'" v-model="consentConfirmed" :disabled="disabled">
        已确认本次营销短信具有用户同意并接受自动追加退订语
      </el-checkbox>
    </el-form>

    <div class="vendor-billing-preview" :class="{ ready: preview }">
      <template v-if="preview">
        <span>后端计费预览</span>
        <strong>预计 {{ preview.quota_cost }} 条</strong>
        <small>最终 {{ preview.final_length }} 字 · {{ preview.est_segments }} 个计费段</small>
      </template>
      <template v-else>
        <span>后端计费预览</span>
        <strong>尚未预检</strong>
        <small>内容变化后必须重新预检。</small>
      </template>
    </div>

    <footer class="vendor-uat-actions">
      <el-button data-testid="uat-preview" :loading="previewing" :disabled="!formReady" @click="runPreview">预检计费</el-button>
      <el-button data-testid="uat-send" type="danger" plain :loading="sending" :disabled="!formReady" @click="send">发送真实 UAT</el-button>
    </footer>
  </section>
</template>
