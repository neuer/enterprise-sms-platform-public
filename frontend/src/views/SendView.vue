<script setup lang="ts">
import "../styles/workspace.css"

import type { UploadRequestOptions } from "element-plus"
import { computed, onMounted, reactive, ref } from "vue"

import {
  previewBilling,
  downloadImportInvalidFile,
  sendWebMessage,
  uploadPhones,
  type BillingPreview,
  type Category,
  type ImportResult,
  type WebMessagePayload,
} from "../api/webMessages"
import { listTemplates, type SmsTemplate } from "../api/templates"
import { getDashboard } from "../api/dashboard"
import SegmentBar from "../components/SegmentBar.vue"
import EmptyState from "../components/EmptyState.vue"

const form = reactive({
  category: "notice" as Category,
  source: "paste" as "paste" | "import",
  contentMode: "content" as "content" | "template",
  mobilesText: "",
  content: "",
  templateId: "",
  templateParamsText: "",
  signName: "",
  scheduledAt: "",
  isTest: false,
  consentConfirmed: false,
  remark: "",
})

const imported = ref<ImportResult | null>(null)
const preview = ref<BillingPreview | null>(null)
const busy = ref(false)
const errorMessage = ref("")
const successMessage = ref("")
const templates = ref<SmsTemplate[]>([])
const templateParams = ref<string[]>([])
const testSendMax = ref<number | null>(null)
const approvedTemplates = computed(() => templates.value.filter((item) => item.vendor_state === "approved"))
const selectedTemplate = computed(() => templates.value.find((item) => item.id === Number(form.templateId)) || null)
const renderedTemplate = computed(() => {
  const template = selectedTemplate.value
  if (!template) return ""
  return template.content.replace(/\{(\d+)\}/g, (placeholder, position: string) => {
    const value = templateParams.value[Number(position) - 1]?.trim()
    return value || placeholder
  })
})

const pastedMobiles = computed(() =>
  form.mobilesText
    .split(/[\s,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean),
)

const recipientCount = computed(() =>
  form.source === "import" ? (imported.value?.valid ?? 0) : pastedMobiles.value.length,
)

const contentReady = computed(() =>
  form.contentMode === "content"
    ? form.content.trim().length > 0
    : Number(form.templateId) > 0,
)

const sendDisabled = computed(
  () =>
    busy.value ||
    recipientCount.value === 0 ||
    !contentReady.value ||
    (form.category === "market" && !form.consentConfirmed),
)
const submitLabel = computed(() => {
  if (successMessage.value) return "已受理 · 写入审计"
  if (preview.value?.approval_required) return "提交审批"
  return form.scheduledAt ? "安排发送" : "立即发送"
})

function contentPayload() {
  if (form.contentMode === "content") return { content: form.content }
  return {
    template_id: Number(form.templateId),
    template_params: templateParams.value.map((value) => value.trim()),
  }
}

async function loadTemplates(): Promise<void> {
  try { templates.value = await listTemplates() }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : "模板列表加载失败" }
}

async function loadUiPolicy(): Promise<void> {
  try {
    const result = await getDashboard()
    const value = result.ui_policy.test_send_max
    testSendMax.value = typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null
  } catch {
    testSendMax.value = null
  }
}

function selectTemplate(value: string | number): void {
  form.templateId = String(value)
  const template = templates.value.find((item) => item.id === Number(value))
  templateParams.value = template?.var_specs.map(() => "") || []
  preview.value = null
}

async function downloadInvalidFile(): Promise<void> {
  if (!imported.value?.invalid_download_url) return
  try {
    const blob = await downloadImportInvalidFile(imported.value.invalid_download_url)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = `sms-import-${imported.value.import_id}-invalid.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : "剔除清单下载失败" }
}

function resetFeedback(): void {
  errorMessage.value = ""
  successMessage.value = ""
}

function chooseCategory(category: Category): void {
  form.category = category
  if (category === "notice") form.consentConfirmed = false
  preview.value = null
  resetFeedback()
}

async function handleUpload(options: UploadRequestOptions): Promise<void> {
  resetFeedback()
  busy.value = true
  try {
    imported.value = await uploadPhones(options.file)
    form.source = "import"
    options.onSuccess(imported.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "号码文件解析失败"
    throw error
  } finally {
    busy.value = false
  }
}

async function refreshPreview(): Promise<void> {
  resetFeedback()
  busy.value = true
  try {
    preview.value = await previewBilling({
      category: form.category,
      ...contentPayload(),
      sign_name: form.signName || undefined,
      accepted_count: recipientCount.value,
      consent_confirmed: form.consentConfirmed,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "预检失败"
  } finally {
    busy.value = false
  }
}

async function submit(): Promise<void> {
  if (sendDisabled.value) return
  resetFeedback()
  busy.value = true
  const payload: WebMessagePayload = {
    category: form.category,
    biz_id: crypto.randomUUID(),
    ...contentPayload(),
    sign_name: form.signName || undefined,
    scheduled_at: form.scheduledAt ? new Date(form.scheduledAt).toISOString() : undefined,
    is_test: form.isTest,
    consent_confirmed: form.consentConfirmed,
    remark: form.remark || undefined,
  }
  if (form.source === "import" && imported.value) payload.import_id = imported.value.import_id
  else payload.mobiles = pastedMobiles.value
  try {
    const result = await sendWebMessage(payload)
    successMessage.value = `批次 ${result.batch_no} 已受理，状态：${result.status}`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "发送受理失败"
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void loadTemplates()
  void loadUiPolicy()
})
</script>

<template>
  <section class="page-heading send-heading">
    <div>
      <p class="eyebrow">DELIVERY / 人工下发</p>
      <h1>人工发送</h1>
      <p>号码仅在受控内存中处理，计费、审批与时间窗均由服务端裁决。</p>
    </div>
    <span class="security-note"><i></i> 敏感数据保护已启用</span>
  </section>

  <div class="send-workbench">
    <section class="send-editor" aria-label="短信编辑区">
      <div class="form-section category-section">
        <div class="section-index">01</div>
        <div class="section-body">
          <header><h2>选择消息类别</h2><small>验证码仅允许 API 渠道</small></header>
          <div class="category-switch">
            <button
              type="button"
              data-testid="category-verify"
              disabled
              aria-describedby="verify-web-hint"
            >
              <b>验证码短信</b><span id="verify-web-hint">仅 API 渠道开放</span>
            </button>
            <button
              type="button"
              data-testid="category-notice"
              :class="{ selected: form.category === 'notice' }"
              @click="chooseCategory('notice')"
            >
              <b>通知短信</b><span>服务通知、业务提醒</span>
            </button>
            <button
              type="button"
              data-testid="category-market"
              :class="{ selected: form.category === 'market' }"
              @click="chooseCategory('market')"
            >
              <b>营销短信</b><span>受时间窗与同意约束</span>
            </button>
          </div>
          <div v-if="form.category === 'market'" class="consent-panel" data-testid="market-consent">
            <el-checkbox v-model="form.consentConfirmed">我确认已获得接收用户的明确同意</el-checkbox>
            <p>同意状态将写入审计；未确认时平台拒绝受理。</p>
          </div>
        </div>
      </div>

      <div class="form-section">
        <div class="section-index">02</div>
        <div class="section-body">
          <header><h2>添加接收号码</h2><small>单次最多 50,000 个</small></header>
          <el-radio-group v-model="form.source" class="compact-radio">
            <el-radio-button value="paste">手工粘贴</el-radio-button>
            <el-radio-button value="import">文件导入</el-radio-button>
          </el-radio-group>
          <el-input
            v-if="form.source === 'paste'"
            v-model="form.mobilesText"
            type="textarea"
            :rows="5"
            resize="vertical"
            placeholder="每行一个手机号，也支持逗号或空格分隔"
          />
          <div v-else class="upload-zone">
            <el-upload
              drag
              action="#"
              accept=".csv,.xlsx"
              :show-file-list="false"
              :http-request="handleUpload"
              :disabled="busy"
            >
              <strong>拖入 CSV / XLSX，或点击选择</strong>
              <span>≤10MB · ≤5万行 · 24小时有效</span>
            </el-upload>
            <div v-if="imported" class="import-summary">
              <b>{{ imported.valid }}</b> 有效
              <span>{{ imported.invalid }} 无效</span>
              <span>{{ imported.duplicate }} 重复</span>
              <span>{{ imported.blacklisted }} 黑名单</span>
              <button v-if="imported.invalid_download_url" data-testid="download-invalid" type="button" class="download-link" @click="downloadInvalidFile">下载剔除清单</button>
            </div>
          </div>
        </div>
      </div>

      <div class="form-section">
        <div class="section-index">03</div>
        <div class="section-body">
          <header><h2>编写发送内容</h2><small>最终内容不得超过 500 字</small></header>
          <el-radio-group v-model="form.contentMode" class="compact-radio">
            <el-radio-button value="content">直接编辑</el-radio-button>
            <el-radio-button value="template">审核模板</el-radio-button>
          </el-radio-group>
          <template v-if="form.contentMode === 'content'">
            <el-input v-model="form.content" type="textarea" :rows="7" maxlength="500" show-word-limit />
          </template>
          <div v-else class="template-fields">
            <el-select v-model="form.templateId" data-testid="template-select" filterable placeholder="选择已审核模板" @change="selectTemplate">
              <el-option v-for="item in approvedTemplates" :key="item.id" :label="item.name" :value="String(item.id)" />
            </el-select>
            <template v-if="selectedTemplate">
              <el-input v-for="spec in selectedTemplate.var_specs" :key="spec.pos" v-model="templateParams[spec.pos - 1]" data-testid="template-param" :maxlength="spec.max_len" :placeholder="`参数 {${spec.pos}}，最多 ${spec.max_len} 字`" />
              <div class="template-render-preview"><span>模板渲染预览</span><p>{{ renderedTemplate }}</p></div>
            </template>
            <EmptyState v-else-if="!approvedTemplates.length" title="当前没有已审核模板" description="请先在模板管理中提交并通过厂商审核。" />
          </div>
          <div class="inline-fields">
            <el-input v-model="form.signName" placeholder="签名（可选）" />
            <el-date-picker
              v-model="form.scheduledAt"
              type="datetime"
              popper-class="qingluan-date-popper"
              value-format="YYYY-MM-DDTHH:mm:ss+08:00"
              placeholder="定时发送（可选）"
            />
          </div>
          <el-input v-model="form.remark" maxlength="200" placeholder="发送备注（可选）" />
          <el-checkbox v-model="form.isTest">
            测试发送（{{ testSendMax === null ? '号码上限暂不可用' : `最多 ${testSendMax} 个号码` }}，豁免营销时间窗）
          </el-checkbox>
        </div>
      </div>
    </section>

    <aside class="send-preview precheck" aria-label="发送预检">
      <div class="preview-head">
        <div><p class="eyebrow">SERVER PRECHECK</p><h2>发送预检</h2></div>
        <el-button :loading="busy" @click="refreshPreview">更新预检</el-button>
      </div>

      <div class="recipient-meter">
        <span>当前受众</span><strong>{{ recipientCount.toLocaleString() }}</strong><small>个号码</small>
      </div>

      <template v-if="preview">
        <dl class="preview-metrics">
          <div><dt>最终字符</dt><dd>{{ preview.final_length }}</dd></div>
          <div><dt>计费条</dt><dd>{{ preview.est_segments }}</dd></div>
          <div><dt>配额消耗</dt><dd>{{ preview.quota_cost }}</dd></div>
        </dl>
        <SegmentBar :parts="preview.segment_parts" />
        <p class="boundary-hint">再增加 {{ preview.next_segment_at }} 个字符进入下一计费段</p>
        <el-alert
          v-if="preview.approval_required"
          title="本次发送达到审批阈值，提交后进入待审批队列"
          type="warning"
          :closable="false"
        />
        <p v-if="preview.unsubscribe_appended" class="compliance-line">服务端已自动补齐退订语</p>
      </template>
      <EmptyState v-else title="等待服务端预检" description="填写号码与内容后，获取计费、配额和审批判断。" />

      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
      <el-alert v-if="successMessage" :title="successMessage" type="success" :closable="false" />

      <el-button
        type="primary"
        size="large"
        class="send-submit"
        data-testid="send-button"
        :disabled="sendDisabled"
        :loading="busy"
        @click="submit"
      >
        {{ submitLabel }}
      </el-button>
      <p class="submit-foot">提交即进入不可重复下发保护链路</p>
    </aside>
  </div>
</template>
