<script setup lang="ts">
import { formatNumber } from "../lib/format"
import { SESSION_CLEARING_EVENT } from "../api/sessionEvents"

import FilterSeg from "../components/FilterSeg.vue"
import { ElMessage } from "element-plus"
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { useRouter } from "vue-router"

import { getDashboard } from "../api/dashboard"
import CodePointInput from "../components/CodePointInput.vue"
import SendPrecheckCards from "../components/SendPrecheckCards.vue"
import SendResultCard from "../components/SendResultCard.vue"
import EmptyState from "../components/EmptyState.vue"
import { useLatestRead } from "../composables/useLatestRead"
import { useMobileLayout } from "../composables/useMobileLayout"
import { useSendDraft } from "./send/useSendDraft"
import { copyText } from "../lib/clipboard"
import { formatDateTime } from "../lib/time"

// 测试环境未安装路由时 useRouter 返回 undefined，跳转入口做空值守卫。
const router = useRouter()

const {
  form,
  imported,
  consumedImportNotice,
  importState,
  importFilename,
  importError,
  preview,
  previewLoading,
  previewError,
  previewStale,
  busy,
  submittedSummary,
  errorMessage,
  errorAdmission,
  sendResult,
  templates,
  approvedTemplates,
  loadTemplates,
  templateParams,
  approvedSigns,
  loadSigns,
  testSendMax,
  selectedTemplate,
  renderedTemplateParts,
  pastedMobiles,
  invalidMobiles,
  dedupedCount,
  duplicateCount,
  recipientCount,
  previewCount,
  testLimitExceeded,
  recipientLimitExceeded,
  scheduleInPast,
  sendDisabled,
  removedDuplicate,
  removedBlacklist,
  finalParts,
  riskLines,
  selectTemplate,
  downloadInvalidFile,
  chooseCategory,
  removeDuplicates,
  resetImport,
  handleUpload,
  submit,
  resetForAnother,
  clearSessionDraft,
  isDisposed,
  markDisposed,
  abortUpload,
  abortPreview,
  clearPreviewTimer,
} = useSendDraft()
const copied = ref(false)
let copiedTimer: number | undefined

// 手机单列时确认栏排在四步表单之后；确认栏进入视口前以吸底条提示号码与计费条，并一键滚到确认栏。
const isMobile = useMobileLayout()
const rail = ref<HTMLElement | null>(null)
const railInView = ref(false)
let railObserver: IntersectionObserver | undefined

onMounted(() => {
  if (typeof IntersectionObserver === "undefined" || !rail.value) return
  railObserver = new IntersectionObserver(([entry]) => {
    railInView.value = entry.isIntersecting
  })
  railObserver.observe(rail.value)
})
onBeforeUnmount(() => railObserver?.disconnect())

function scrollToRail(): void {
  rail.value?.scrollIntoView({ behavior: "smooth", block: "start" })
}

const submitLabel = computed(() => {
  const current = previewStale.value ? null : preview.value
  const cost = current ? ` · ${formatNumber(current.quota_cost)} 计费条` : ""
  if (form.isTest) return `立即发送${cost}`
  if (current?.approval_required) return `提交审批${cost}`
  if (form.scheduleEnabled || current?.deferred_reason === "market_window") return `安排发送${cost}`
  return `立即发送${cost}`
})

const testSendHint = computed(() =>
  testSendMax.value === null
    ? "号码上限暂不可用"
    : `≤${testSendMax.value} 个号码 · 豁免审批与营销时间窗 · 其余管控照常`,
)

const uiPolicyRead = useLatestRead()
async function loadUiPolicy(): Promise<void> {
  const signal = uiPolicyRead.start()
  try {
    const result = await getDashboard(signal)
    if (signal.aborted) return
    const value = result.ui_policy.test_send_max
    testSendMax.value = typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null
  } catch {
    if (signal.aborted) return
    testSendMax.value = null
    ElMessage.warning("测试发送号码上限读取失败，测试发送的号码上限提示暂不可用；正式发送不受影响")
  }
}

/** 模板管理「用于发送」入口：/send?template_id=N 预选已审核模板；未通过审核或不存在则不预选。 */
function applyTemplateQuery(): void {
  if (busy.value || isDisposed()) return
  const raw = router?.currentRoute?.value?.query?.template_id
  const id = Array.isArray(raw) ? raw[0] : raw
  if (!id) return
  const template = templates.value.find((item) => String(item.id) === id && item.vendor_state === "approved")
  if (!template) return
  form.contentMode = "template"
  selectTemplate(template.id)
}

function formatExpiry(value: string): string {
  // 锁 Asia/Shanghai（lib/time 单点）：非 +08:00 环境下 toLocaleString 会显示错误时间。
  return formatDateTime(value, value)
}

async function copyBatchNo(): Promise<void> {
  if (!sendResult.value) return
  if (await copyText(sendResult.value.batch_no)) {
    copied.value = true
    window.clearTimeout(copiedTimer)
    copiedTimer = window.setTimeout(() => {
      copiedTimer = undefined
      copied.value = false
    }, 1600)
  } else {
    errorMessage.value = "复制失败，请手动选择批次号"
  }
}

function goBatches(): void {
  void router?.push("/batches")
}

function goOpsQueue(): void {
  void router?.push("/ops?tab=queue")
}

onMounted(() => {
  window.addEventListener(SESSION_CLEARING_EVENT, clearSessionDraft)
  void loadTemplates().then(applyTemplateQuery)
  void loadSigns()
  void loadUiPolicy()
})

onBeforeUnmount(() => {
  clearSessionDraft()
  markDisposed()
  abortPreview()
  abortUpload()
  window.removeEventListener(SESSION_CLEARING_EVENT, clearSessionDraft)
  clearPreviewTimer()
  window.clearTimeout(copiedTimer)
})
</script>

<template>
  <section class="page-heading send-heading">
    <div>
      <p class="eyebrow">DELIVERY / 人工下发</p>
      <h1>人工发送</h1>
      <p>号码仅在受控内存中处理，计费、审批与时间窗均由服务端裁决。</p>
    </div>
    <span class="security-note"><i></i> 敏感数据保护已启用 · 预检自动更新</span>
  </section>

  <p v-if="busy" role="status">正在处理本次请求，草稿暂时锁定。</p>
  <div class="send-workbench" :aria-busy="busy">
    <div class="send-editor" aria-label="短信编辑区">
      <section class="form-panel">
        <header>
          <span class="form-index">01</span>
          <h2>消息类别</h2>
          <small>类别策略矩阵决定队列、时间窗、审批与频控</small>
        </header>
        <div class="category-switch">
          <button
            type="button"
            class="notice"
            data-testid="category-notice"
            :class="{ on: form.category === 'notice', selected: form.category === 'notice' }"
            :disabled="busy"
            @click="chooseCategory('notice')"
          >
            <b>通知短信<span class="cat-tag notice">NOTICE</span></b>
            <small>实时通道 · 黑名单默认拦截<br />是否需要审批以服务端预检为准</small>
          </button>
          <button
            type="button"
            class="market"
            data-testid="category-market"
            :class="{ on: form.category === 'market', selected: form.category === 'market' }"
            :disabled="busy"
            @click="chooseCategory('market')"
          >
            <b>营销短信<span class="cat-tag market">MARKET</span></b>
            <small>批量通道 · 默认 08:00–21:00 · 强制退订语<br />审批以预检为准 · 频控按配置执行</small>
          </button>
          <!-- verify 仅 API 渠道（PRD FR-00），Web 人工发送不展示验证码类别卡 -->
        </div>
      </section>

      <section class="form-panel">
        <header>
          <span class="form-index">02</span>
          <h2>收信号码</h2>
          <small>单次最多 50,000 个</small>
          <FilterSeg
            v-model="form.source"
            :options="[
              ...[{ label: '手工粘贴', value: 'paste' as const }],
              ...[{ label: '文件导入', value: 'import' as const }],
            ]"
            class="filter-seg--pill"
            label="号码来源"
            :disabled="busy"
          />
        </header>
        <template v-if="form.source === 'paste'">
          <el-input
            v-model="form.mobilesText"
            type="textarea"
            :rows="5"
            resize="vertical"
            placeholder="每行一个手机号，也支持逗号或空格分隔"
            aria-label="收信号码，每行一个手机号，也支持逗号或空格分隔"
            :disabled="busy"
          />
          <div v-if="pastedMobiles.length" class="phone-stats" data-testid="phone-stats">
            <span
              >共 <b>{{ formatNumber(pastedMobiles.length) }}</b></span
            >
            <span
              >去重后 <b>{{ formatNumber(dedupedCount) }}</b></span
            >
            <span class="bad"
              >格式无效 <b>{{ formatNumber(invalidMobiles.length) }}</b></span
            >
            <span
              >重复 <b>{{ formatNumber(duplicateCount) }}</b></span
            >
            <span class="act">
              <button v-if="duplicateCount > 0" :disabled="busy" type="button" @click="removeDuplicates"
                >移除重复</button
              >
            </span>
          </div>
          <p v-if="invalidMobiles.length" class="mobiles-invalid-hint" data-testid="invalid-mobiles-hint">
            {{ invalidMobiles.length }} 个号码格式无效（需为 1 开头的 11 位数字），例如「{{
              invalidMobiles[0]
            }}」；请修正后再提交。
          </p>
          <p v-if="recipientLimitExceeded" class="mobiles-invalid-hint" data-testid="recipient-limit-hint">
            超出单次 50,000 个号码上限（当前 {{ formatNumber(recipientCount) }} 个）；请删减或分批提交。
          </p>
        </template>
        <div v-else class="upload-zone">
          <el-upload
            v-if="importState === 'idle'"
            drag
            action="#"
            accept=".csv,.xlsx"
            :show-file-list="false"
            :http-request="handleUpload"
            :disabled="busy"
          >
            <!-- el-upload 不透传 aria-label；根节点 role=button 的可访问名取自下列可见文本 -->
            <strong>拖入 CSV / XLSX，或点击选择</strong>
            <span>≤10MB · ≤5万行 · 24小时有效</span>
          </el-upload>
          <div v-if="importState === 'parsing'" class="import-box" data-testid="import-parsing">
            <div class="import-processing">
              <i class="import-spinner" aria-hidden="true"></i>
              <div>
                <p>正在解析 {{ importFilename }} …</p>
                <small>大文件流式解析，请稍候</small>
              </div>
            </div>
          </div>
          <div v-if="importState === 'failed'" class="import-box failed" data-testid="import-failed">
            <p>{{ importError || "号码文件解析失败" }}</p>
            <button type="button" class="text-action" :disabled="busy" @click="resetImport">重新上传</button>
          </div>
          <div v-if="importState === 'ready' && imported" class="import-box" data-testid="import-ready">
            <div class="import-ready">
              <div class="cell"
                ><span>有效</span><b>{{ formatNumber(imported.valid) }}</b></div
              >
              <div class="cell" :class="{ bad: imported.invalid > 0 }"
                ><span>格式无效</span><b>{{ formatNumber(imported.invalid) }}</b></div
              >
              <div class="cell" :class="{ bad: imported.duplicate > 0 }"
                ><span>重复</span><b>{{ formatNumber(imported.duplicate) }}</b></div
              >
              <div class="cell" :class="{ bad: imported.blacklisted > 0 }"
                ><span>黑名单</span><b>{{ formatNumber(imported.blacklisted) }}</b></div
              >
              <div class="act">
                <button
                  v-if="imported.invalid_download_url"
                  data-testid="download-invalid"
                  type="button"
                  :disabled="busy"
                  @click="downloadInvalidFile"
                  >下载剔除清单</button
                >
                <button type="button" :disabled="busy" @click="resetImport">重新上传</button>
              </div>
            </div>
            <p class="import-meta">
              {{ importFilename || "导入文件" }} · 解析完成 · 导入包 24 小时内有效（至
              {{ formatExpiry(imported.expires_at) }}）· 频控剔除在受理时判定
            </p>
          </div>
          <p v-if="consumedImportNotice" class="test-limit-hint" data-testid="consumed-import-notice" role="status">
            {{ consumedImportNotice }}
          </p>
        </div>
      </section>

      <section class="form-panel">
        <header>
          <span class="form-index">03</span>
          <h2>发送内容</h2>
          <small>最终内容（含签名与退订语）不超过 500 字</small>
          <FilterSeg
            v-model="form.contentMode"
            :options="[
              ...[{ label: '直接编辑', value: 'content' as const }],
              ...[{ label: '审核模板', value: 'template' as const }],
            ]"
            class="filter-seg--pill"
            label="内容来源"
            :disabled="busy"
          />
        </header>
        <CodePointInput
          v-if="form.contentMode === 'content'"
          v-model="form.content"
          multiline
          :rows="4"
          :max-code-points="500"
          aria-label="短信内容，最终内容含签名与退订语不超过 500 字"
          test-id="send-content"
          :disabled="busy"
        />
        <div v-else class="template-fields">
          <el-select
            v-model="form.templateId"
            data-testid="template-select"
            filterable
            placeholder="选择已审核模板"
            :disabled="busy"
            @change="selectTemplate"
          >
            <el-option v-for="item in approvedTemplates" :key="item.id" :label="item.name" :value="String(item.id)" />
          </el-select>
          <template v-if="selectedTemplate">
            <div class="tpl-params">
              <CodePointInput
                v-for="spec in selectedTemplate.var_specs"
                :key="spec.pos"
                v-model="templateParams[spec.pos - 1]"
                :max-code-points="spec.max_len"
                :placeholder="`参数 {${spec.pos}}，最多 ${spec.max_len} 字`"
                test-id="template-param"
                :disabled="busy"
              />
            </div>
            <div class="template-render-preview">
              <span>模板渲染预览</span>
              <p>
                <template v-for="(part, index) in renderedTemplateParts" :key="index">
                  <em v-if="part.highlight">{{ part.text }}</em
                  ><template v-else>{{ part.text }}</template>
                </template>
              </p>
            </div>
          </template>
          <EmptyState
            v-else-if="!approvedTemplates.length"
            title="当前没有已审核模板"
            description="请先在模板管理中提交并通过厂商审核。"
          />
        </div>
        <div class="inline-fields">
          <el-select
            v-model="form.signName"
            data-testid="sign-select"
            aria-label="签名"
            clearable
            placeholder="不指定 · 用应用默认签名"
            :disabled="busy"
          >
            <el-option
              v-for="item in approvedSigns"
              :key="item.id"
              :label="`签名：${item.name}（已过审）`"
              :value="item.name"
            />
          </el-select>
          <el-input
            v-model="form.remark"
            maxlength="200"
            placeholder="发送备注（可选，写入批次与审计）"
            aria-label="发送备注（可选，写入批次与审计）"
            :disabled="busy"
          />
        </div>
      </section>

      <section class="form-panel">
        <header><span class="form-index">04</span><h2>发送选项</h2></header>
        <div class="opt-row">
          <label class="opt">
            <input v-model="form.scheduleEnabled" type="checkbox" :disabled="busy || form.isTest" />
            <span>定时发送<small>不勾选为立即发送；营销窗外自动转定时</small></span>
          </label>
          <el-date-picker
            v-model="form.scheduledAt"
            type="datetime"
            popper-class="qingluan-date-popper"
            value-format="YYYY-MM-DDTHH:mm:ss+08:00"
            placeholder="选择发送时间（必填）"
            aria-label="定时发送时间"
            :disabled="busy || form.isTest || !form.scheduleEnabled"
          />
          <label class="opt">
            <input v-model="form.isTest" type="checkbox" :disabled="busy" />
            <span
              >测试发送<small>{{ testSendHint }}</small></span
            >
          </label>
        </div>
        <p v-if="scheduleInPast" class="test-limit-hint" data-testid="schedule-past-hint">
          定时时间早于当前时刻；请选择未来的发送时间，或取消定时立即发送。
        </p>
        <p v-if="testLimitExceeded" class="test-limit-hint" data-testid="test-limit-hint">
          测试发送最多 {{ testSendMax }} 个号码，当前
          {{ formatNumber(recipientCount) }} 个；请删减号码，或取消测试发送按正式批次提交。
        </p>
      </section>
    </div>

    <aside ref="rail" class="send-preview precheck send-rail" aria-label="发送确认">
      <span v-if="previewLoading" class="preview-state">更新中…</span>
      <span v-else-if="previewStale" class="preview-state stale">已过期</span>

      <SendPrecheckCards
        :preview="preview"
        :final-parts="finalParts"
        :audience-count="previewCount"
        :removed-duplicate="removedDuplicate"
        :removed-blacklist="removedBlacklist"
        :risk-lines="riskLines"
      />

      <EmptyState
        v-if="!preview && !previewLoading"
        :title="form.category === 'market' && !form.consentConfirmed ? '勾选同意声明后自动预检' : '等待服务端预检'"
        description="填写号码与内容后，自动获取计费、配额和审批判断。"
      />
      <p v-if="previewError" class="preview-error">{{ previewError }}</p>

      <label v-if="form.category === 'market'" class="consent-panel" data-testid="market-consent">
        <input v-model="form.consentConfirmed" type="checkbox" :disabled="busy" />
        <span>
          <b>我确认以上收信人已同意接收营销信息。</b>
          <p>勾选行为与操作人将写入审计日志；未确认时平台拒绝受理（422 CONSENT_REQUIRED）。</p>
        </span>
      </label>

      <SendResultCard
        v-if="sendResult"
        :result="sendResult"
        :busy="busy"
        :copied="copied"
        :summary="submittedSummary"
        @copy="copyBatchNo"
        @view-batches="goBatches"
        @reset="resetForAnother"
      />

      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false">
        <template v-if="errorAdmission" #default
          ><el-button link type="primary" data-testid="send-admission-ops-link" @click="goOpsQueue"
            >前往运维中心查看队列</el-button
          ></template
        >
      </el-alert>

      <div v-if="!sendResult" class="submit-wrap">
        <el-button
          type="primary"
          size="large"
          class="send-submit"
          :class="{ approval: preview?.approval_required }"
          data-testid="send-button"
          :disabled="sendDisabled"
          :loading="busy"
          @click="submit"
        >
          {{ submitLabel }}
        </el-button>
        <p class="submit-foot">提交即生成批次并预扣配额 · 24h 幂等键防重复下发</p>
      </div>
      <p v-else class="submit-foot">提交即生成批次并预扣配额 · 24h 幂等键防重复下发</p>
    </aside>
    <div v-if="isMobile && !sendResult && !railInView" class="send-jumpbar" data-testid="send-jumpbar">
      <span
        ><b>{{ formatNumber(recipientCount) }}</b> 个号码<template v-if="preview">
          · <b>{{ formatNumber(preview.quota_cost) }}</b> 计费条</template
        ></span
      >
      <el-button type="primary" @click="scrollToRail">确认并提交</el-button>
    </div>
  </div>
</template>
