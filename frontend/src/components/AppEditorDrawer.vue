<script setup lang="ts">
import { ElMessage } from "element-plus"
import { computed, onMounted, reactive, ref } from "vue"

import { estimateWorstCaseCapacity, parseFrequencyOverride, type AppPayload, type ManagedApp } from "../api/apps"
import { listSigns } from "../api/signs"
import { useApprovedResources } from "../composables/useApprovedResources"
import { errorText } from "../lib/error"
import { formatNumber } from "../lib/format"
import type { MessageCategory } from "../lib/labels"

/**
 * 应用新建/编辑抽屉：持有表单状态、签名下拉与本地校验，校验通过后以 save 事件交出载荷；
 * 提交接口、一次性凭据展示与列表刷新由应用管理页负责。打开前须先调用 prepare 回填表单。
 */
const open = defineModel<boolean>({ default: false })
defineProps<{ saving: boolean }>()
const emit = defineEmits<{ save: [body: AppPayload, name: string, appId: number | null] }>()

/** 正在编辑的应用 id；null 表示新建。 */
const targetId = ref<number | null>(null)
const creating = computed(() => targetId.value === null)

const form = reactive({
  name: "",
  dept: "",
  allowed_categories: ["notice"] as MessageCategory[],
  default_sign: "",
  daily_quota: 0,
  rate_limit_per_min: 60,
  recipient_limit_per_min: 10000,
  segment_limit_per_min: 10000,
  max_in_flight_chunks: 200,
  allow_market_api_bulk: false,
  blacklist_check: true,
  freq_override: "",
  allowed_ips: "",
  ip_allowlist_exempt_until: "",
  unlimited_quota_exempt_until: "",
  admission_exempt_note: "",
  callback_url: "",
  callback_report_enabled: false,
  status: 1 as 0 | 1,
})

/** 频控覆盖输入失焦前的内联校验；保存时仍由 payload() 兜底。 */
const freqOverrideError = computed(() => {
  if (!form.freq_override.trim()) return ""
  try {
    parseFrequencyOverride(form.freq_override)
    return ""
  } catch (error) {
    return errorText(error, "频控覆盖 JSON 无效")
  }
})

function resetForm(): void {
  Object.assign(form, {
    name: "",
    dept: "",
    allowed_categories: ["notice"],
    default_sign: "",
    daily_quota: 0,
    rate_limit_per_min: 60,
    recipient_limit_per_min: 10000,
    segment_limit_per_min: 10000,
    max_in_flight_chunks: 200,
    allow_market_api_bulk: false,
    blacklist_check: true,
    freq_override: "",
    allowed_ips: "",
    ip_allowlist_exempt_until: "",
    unlimited_quota_exempt_until: "",
    admission_exempt_note: "",
    callback_url: "",
    callback_report_enabled: false,
    status: 1,
  })
}

function fillForm(item: ManagedApp): void {
  Object.assign(form, {
    ...item,
    default_sign: item.default_sign || "",
    allowed_ips: item.allowed_ips.join("\n"),
    ip_allowlist_exempt_until: item.ip_allowlist_exempt_until || "",
    unlimited_quota_exempt_until: item.unlimited_quota_exempt_until || "",
    admission_exempt_note: item.admission_exempt_note || "",
    callback_url: item.callback_url || "",
    freq_override: item.freq_override ? JSON.stringify(item.freq_override) : "",
  })
}

/** 按目标应用回填或重置表单；编辑期间列表刷新不覆盖未保存输入。 */
function prepare(app: ManagedApp | null): void {
  targetId.value = app?.id ?? null
  if (app) fillForm(app)
  else resetForm()
}

defineExpose({ prepare })

const {
  approved: approvedSigns,
  loading: signsLoading,
  unavailable: signsUnavailable,
  load: loadApprovedSigns,
} = useApprovedResources(listSigns, (error) => ElMessage.error(errorText(error, "已通过签名清单加载失败")))

/** 当前默认签名不在已通过清单时补一个遗留项，避免下拉显示原始值或被静默清空。 */
const legacySign = computed(() => {
  const value = form.default_sign.trim()
  if (!value) return null
  return approvedSigns.value.some((item) => item.name === value) ? null : value
})

function parseAllowedIps(input: string): string[] {
  return input
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function payload(): AppPayload {
  const override = parseFrequencyOverride(form.freq_override)
  return {
    dept: form.dept.trim(),
    allowed_categories: form.allowed_categories,
    default_sign: form.default_sign.trim() || null,
    daily_quota: form.daily_quota,
    rate_limit_per_min: form.rate_limit_per_min,
    recipient_limit_per_min: form.recipient_limit_per_min,
    segment_limit_per_min: form.segment_limit_per_min,
    max_in_flight_chunks: form.max_in_flight_chunks,
    allow_market_api_bulk: form.allow_market_api_bulk,
    blacklist_check: form.blacklist_check,
    freq_override: override,
    callback_url: form.callback_url.trim() || null,
    allowed_ips: parseAllowedIps(form.allowed_ips),
    ip_allowlist_exempt_until: form.ip_allowlist_exempt_until.trim() || null,
    unlimited_quota_exempt_until: form.unlimited_quota_exempt_until.trim() || null,
    admission_exempt_note: form.admission_exempt_note.trim() || null,
    callback_report_enabled: form.callback_report_enabled,
    status: form.status,
  }
}

const worstCase = computed(() => estimateWorstCaseCapacity(form))

function submit(): void {
  if (!form.name.trim() || !form.dept.trim() || !form.allowed_categories.length) {
    ElMessage.warning("请填写应用名、部门并选择至少一个类别")
    return
  }
  let body: AppPayload
  try {
    body = payload()
  } catch (error) {
    ElMessage.error(errorText(error, "应用保存失败"))
    return
  }
  emit("save", body, form.name.trim(), targetId.value)
}

onMounted(() => {
  void loadApprovedSigns()
})
</script>

<template>
  <el-drawer v-model="open" class="apps-drawer apps-editor-drawer" size="min(560px, 92vw)" :teleported="false">
    <template #header>
      <div class="apps-drawer-head">
        <div class="apps-drawer-title">{{ creating ? "新建应用" : "编辑应用" }}</div>
        <code>{{
          creating
            ? "创建成功后 API Key 与回调密钥仅展示一次，请立即保存"
            : `正在编辑「${form.name}」· 应用名创建后不可修改`
        }}</code>
      </div>
    </template>
    <el-form label-position="top" @submit.prevent="submit">
      <section class="apps-form-sec">
        <h3>基本信息</h3>
        <el-form-item label="应用名" required>
          <el-input v-model="form.name" :disabled="!creating" maxlength="64" autocomplete="off" />
          <small class="field-rule">1–64 字符，全局唯一，创建后不可修改。</small>
        </el-form-item>
        <el-form-item label="部门" required>
          <el-input v-model="form.dept" maxlength="128" />
          <small class="field-rule">1–128 字符，用于部门级日配额归集。</small>
        </el-form-item>
        <el-form-item label="允许类别" required>
          <el-checkbox-group v-model="form.allowed_categories">
            <el-checkbox value="verify">验证码</el-checkbox>
            <el-checkbox value="notice">通知</el-checkbox>
            <el-checkbox value="market">营销</el-checkbox>
          </el-checkbox-group>
          <small class="field-rule"
            >默认仅通知。验证码/营销须显式勾选；未授权类别的发送请求返回 403 CATEGORY_NOT_ALLOWED。</small
          >
        </el-form-item>
        <el-form-item label="默认签名">
          <el-select
            v-model="form.default_sign"
            data-testid="default-sign-select"
            clearable
            filterable
            :loading="signsLoading"
            :placeholder="approvedSigns.length ? '从已通过签名中选择' : '暂无已通过签名'"
            class="apps-form-select"
          >
            <el-option v-for="sign in approvedSigns" :key="sign.id" :value="sign.name" :label="`【${sign.name}】`" />
            <el-option v-if="legacySign" :value="legacySign" :label="`【${legacySign}】（未通过审核的遗留值）`" />
          </el-select>
          <small class="field-rule"
            >仅可选择签名管理中厂商状态为「已通过」的签名；请求未指定签名时使用，请求内显式签名优先。清空表示不设置。</small
          >
          <small v-if="signsUnavailable" class="field-rule">
            签名清单加载失败，<el-button link type="primary" data-testid="signs-retry" @click="loadApprovedSigns"
              >重试</el-button
            >；保存前请确认可选范围。
          </small>
        </el-form-item>
      </section>

      <section class="apps-form-sec">
        <h3>配额与策略</h3>
        <div class="apps-form-2col">
          <el-form-item label="日配额（计费条）">
            <el-input-number v-model="form.daily_quota" :min="0" :max="100000000" />
            <small class="field-rule">0 = 不限量，最大 100,000,000。</small>
          </el-form-item>
          <el-form-item label="每分钟限流">
            <el-input-number v-model="form.rate_limit_per_min" :min="1" :max="60000" />
            <small class="field-rule">1–60,000 次请求/分钟。</small>
          </el-form-item>
        </div>
        <div class="apps-form-2col">
          <el-form-item label="每分钟号码上限">
            <el-input-number v-model="form.recipient_limit_per_min" :min="1" :max="100000000" />
          </el-form-item>
          <el-form-item label="每分钟计费条上限">
            <el-input-number v-model="form.segment_limit_per_min" :min="1" :max="100000000" />
          </el-form-item>
        </div>
        <div class="apps-form-2col">
          <el-form-item label="在途分片上限">
            <el-input-number v-model="form.max_in_flight_chunks" :min="1" :max="100000" />
          </el-form-item>
          <el-form-item label="营销 API 大批量预授权">
            <el-switch v-model="form.allow_market_api_bulk" />
            <small class="field-rule">关闭时，API 营销达到审批阈值将被 403 拒绝，不会转入人工审批。</small>
          </el-form-item>
        </div>
        <div class="apps-form-alert" data-testid="worst-case-capacity">
          最坏能力：每分钟最多 {{ formatNumber(worstCase.recipientsPerMin) }} 个号码、
          {{ formatNumber(worstCase.segmentsPerMin) }} 计费条；每日
          {{
            worstCase.dailySegments === null
              ? "不限量（生产须豁免）"
              : `${formatNumber(worstCase.dailySegments)} 计费条`
          }}。 单请求最多 10,000 号码，1×10,000 与 100×100 按同一成本计入。
        </div>
        <div v-if="form.daily_quota === 0" class="apps-form-alert">
          日配额为 0 表示不限量。生产保存必须填写未过期豁免与原因，否则无法保存。
        </div>
        <el-form-item label="黑名单检查">
          <el-switch v-model="form.blacklist_check" />
          <small class="field-rule">关闭后该应用号码不执行黑名单剔除。</small>
        </el-form-item>
        <el-form-item label="频控覆盖 JSON" :error="freqOverrideError || undefined">
          <el-input
            v-model="form.freq_override"
            data-testid="freq-override"
            type="textarea"
            placeholder='例如 {"verify_per_minute":2,"verify_per_day":20,"market_per_day":1}'
          />
          <small class="field-rule"
            >留空用系统默认；仅 verify_per_minute（1–100）/ verify_per_day（1–10,000）/
            market_per_day（1–1,000），值为正整数。</small
          >
        </el-form-item>
      </section>

      <section class="apps-form-sec">
        <h3>安全与回调</h3>
        <el-form-item label="来源 IP 白名单（每行一个 IP/CIDR，最多 50 条）">
          <div v-if="!form.allowed_ips.trim()" class="apps-form-alert apps-form-alert--verm">
            白名单为空表示全网放行。生产环境必须填写 CIDR，或提供未过期豁免与原因。
          </div>
          <el-input
            v-model="form.allowed_ips"
            data-testid="allowed-ips-input"
            type="textarea"
            placeholder="203.0.113.0/24"
          />
          <small class="field-rule">单 IP 自动归一化为 /32；留空仅开发/测试或已登记豁免可用，保存时校验格式。</small>
        </el-form-item>
        <el-form-item label="豁免到期（空白名单 / 无限配额）">
          <el-input
            v-model="form.ip_allowlist_exempt_until"
            placeholder="空白名单豁免 ISO8601，如 2026-09-10T08:00:00+08:00"
          />
          <el-input
            v-model="form.unlimited_quota_exempt_until"
            placeholder="无限配额豁免 ISO8601"
            class="apps-form-stacked"
          />
          <el-input
            v-model="form.admission_exempt_note"
            maxlength="200"
            placeholder="豁免原因（生产必填）"
            class="apps-form-stacked"
          />
        </el-form-item>
        <el-form-item label="回调 URL">
          <el-input v-model="form.callback_url" placeholder="https://" />
          <small class="field-rule">须落在内网 CIDR 白名单，生产仅 HTTPS；留空表示不推送回调。</small>
        </el-form-item>
        <el-form-item label="明细回调">
          <el-switch v-model="form.callback_report_enabled" />
          <small class="field-rule">按消息粒度推送明细回调，开启前需先配置回调 URL。</small>
        </el-form-item>
      </section>
    </el-form>
    <template #footer>
      <div class="apps-drawer-foot">
        <small class="apps-form-audit">保存即记审计（{{ creating ? "app_create" : "app_update" }}）</small>
        <span class="apps-foot-sp"></span>
        <el-button @click="open = false">取消</el-button>
        <el-button data-testid="save-app" type="primary" :loading="saving" @click="submit">{{
          creating ? "创建应用" : "保存"
        }}</el-button>
      </div>
    </template>
  </el-drawer>
</template>
