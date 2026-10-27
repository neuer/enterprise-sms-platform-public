import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { effectScope, ref } from "vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { vi } from "vitest"

import AuditView from "../src/views/AuditView.vue"
import BatchView from "../src/views/BatchView.vue"
import MessageView from "../src/views/MessageView.vue"
import ReplyView from "../src/views/ReplyView.vue"
import { useBatchFilters } from "../src/views/batch/useBatchFilters"
import { useSessionStore } from "../src/stores/session"
import {
  dateForShanghaiClock,
  dateForShanghaiDayStart,
  rangeToIsoParams,
  SHANGHAI_CIVIL_VALUE_FORMAT,
  shanghaiCivilToApi,
  trailingShanghaiRange,
} from "../src/lib/time"

function response(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
  }
}

/** 模拟日期控件 value-format：取本地钟面，不读取该时刻的 UTC 偏移。 */
function civilFromPicker(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0")
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

describe("上海钟面时间筛选", () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it("同一显示区间在任意进程时区都解析为 +08:00，倒序和非法日期保持原规则", () => {
    expect(rangeToIsoParams(null)).toEqual({ start: undefined, end: undefined })
    expect(rangeToIsoParams(["2026-10-26 09:00:00", "2026-10-26 10:00:00"])).toEqual({
      start: "2026-10-26T09:00:00+08:00",
      end: "2026-10-26T10:00:00+08:00",
    })
    expect(rangeToIsoParams(["2026-10-26 10:00", "2026-10-26 09:00:59"])).toEqual({
      start: "2026-10-26T10:00:00+08:00",
      end: "2026-10-26T09:00:59+08:00",
    })
    expect(shanghaiCivilToApi("2026-03-08 02:30:00")).toBe("2026-03-08T02:30:00+08:00")
    expect(shanghaiCivilToApi("2026-01-31 23:59:59")).toBe("2026-01-31T23:59:59+08:00")
    expect(shanghaiCivilToApi("2026-02-31 00:00:00")).toBeUndefined()
    expect(rangeToIsoParams(["2026-02-31 00:00:00", "2026-03-01 00:00:00"])).toEqual({
      start: undefined,
      end: undefined,
    })
  })

  it("今天从上海自然日零点起，相对区间保持真实时长并跨日", () => {
    const now = new Date("2026-10-26T16:30:00Z")
    const today = [dateForShanghaiDayStart(now), dateForShanghaiClock(now)] as const
    expect(rangeToIsoParams([civilFromPicker(today[0]), civilFromPicker(today[1])])).toEqual({
      start: "2026-10-27T00:00:00+08:00",
      end: "2026-10-27T00:30:00+08:00",
    })
    const hour = trailingShanghaiRange(3_600_000, now)
    expect(rangeToIsoParams([civilFromPicker(hour[0]), civilFromPicker(hour[1])])).toEqual({
      start: "2026-10-26T23:30:00+08:00",
      end: "2026-10-27T00:30:00+08:00",
    })
    const monthEnd = trailingShanghaiRange(3_600_000, new Date("2026-01-31T16:30:00Z"))
    expect(rangeToIsoParams(monthEnd.map(civilFromPicker) as [string, string])).toEqual({
      start: "2026-01-31T23:30:00+08:00",
      end: "2026-02-01T00:30:00+08:00",
    })
  })

  it("审计今天快捷项与清空后的查询都按上海钟面时间", async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-10-26T16:30:00Z"))
    const fetch = vi.fn(async (url: string) =>
      response(String(url).includes("/actions") ? [] : { total: 0, items: [] }),
    )
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AuditView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const picker = wrapper.getComponent({ name: "ElDatePicker" })
    expect(picker.props("valueFormat")).toBe(SHANGHAI_CIVIL_VALUE_FORMAT)
    const today = (picker.props("shortcuts") as { text: string; value: () => [Date, Date] }[]).find(
      (item) => item.text === "今天",
    )
    expect(today).toBeTruthy()
    const [start, end] = today!.value()
    const vm = wrapper.vm as unknown as { timeRange: [string, string] | null }
    vm.timeRange = [civilFromPicker(start), civilFromPicker(end)]
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    const queried = decodeURIComponent(String(fetch.mock.calls.at(-1)?.[0]))
    expect(queried).toContain("start=2026-10-27T00:00:00+08:00")
    expect(queried).toContain("end=2026-10-27T00:30:00+08:00")
    vm.timeRange = null
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    expect(decodeURIComponent(String(fetch.mock.calls.at(-1)?.[0]))).not.toContain("start=")
    wrapper.unmount()
  })

  it("批次、号码和回复查询提交同一上海区间", async () => {
    const fetch = vi.fn(async () => response({ total: 0, items: [], status_counts: {} }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/", component: { template: "<div />" } }],
    })
    const batch = mount(BatchView, { global: { plugins: [pinia, ElementPlus, router] } })
    await flushPromises()
    expect(batch.getComponent({ name: "ElDatePicker" }).props("valueFormat")).toBe(SHANGHAI_CIVIL_VALUE_FORMAT)
    const batchVm = batch.vm as unknown as { range: [string, string] | null }
    batchVm.range = ["2026-10-26 09:00:00", "2026-10-26 10:00:00"]
    await batch.get("form").trigger("submit")
    await flushPromises()
    const batchCall = fetch.mock.calls.at(-1) as [string, RequestInit] | undefined
    expect(batchCall).toBeTruthy()
    expect(decodeURIComponent(String(batchCall?.[0]))).toContain("start=2026-10-26T09:00:00+08:00")
    batch.unmount()

    const scope = effectScope()
    const filters = scope.run(() => useBatchFilters(ref(false)))!
    filters.range.value = ["2026-02-31 00:00:00", "2026-03-01 00:00:00"]
    expect(filters.listQuery(1).start).toBeUndefined()
    filters.clearFilters()
    expect(filters.range.value).toBeNull()
    scope.stop()

    const message = mount(MessageView, { global: { plugins: [pinia, ElementPlus] } })
    const messageVm = message.vm as unknown as { phone: string; range: [string, string] | null }
    messageVm.phone = "13800138000"
    messageVm.range = ["2026-10-26 09:00", "2026-10-26 10:00:00"]
    await message.get("form").trigger("submit")
    await flushPromises()
    const messageCall = fetch.mock.calls.at(-1) as [string, RequestInit] | undefined
    expect(JSON.parse(String(messageCall?.[1]?.body))).toMatchObject({
      start: "2026-10-26T09:00:00+08:00",
      end: "2026-10-26T10:00:00+08:00",
    })
    message.unmount()

    const reply = mount(ReplyView, { global: { plugins: [pinia, ElementPlus, router] } })
    await flushPromises()
    const replyVm = reply.vm as unknown as { range: [string, string] | null }
    replyVm.range = ["2026-10-26 09:00:00", "2026-10-26 10:00:00"]
    await reply.get("form").trigger("submit")
    await flushPromises()
    const replyCall = fetch.mock.calls.at(-1) as [string, RequestInit] | undefined
    expect(JSON.parse(String(replyCall?.[1]?.body))).toMatchObject({
      start: "2026-10-26T09:00:00+08:00",
      end: "2026-10-26T10:00:00+08:00",
    })
    reply.unmount()
  })
})
