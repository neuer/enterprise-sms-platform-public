import { effectScope, ref, type Ref } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useDebouncedEntries, type DebouncedEntries } from "../src/composables/useDebouncedEntries"

/** 与黑名单/发送页同口径的拆分器，供计数断言。 */
function parsePhones(text: string): string[] {
  return text
    .split(/[\s,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean)
}

function mountEntries(text: Ref<string>, parse: (value: string) => string[] = parsePhones) {
  const scope = effectScope()
  const entries = scope.run(() => useDebouncedEntries(text, { parse })) as DebouncedEntries
  return { scope, ...entries }
}

describe("批量录入防抖解析 useDebouncedEntries", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("小文本每次变更同步解析，校验计数即时反馈", () => {
    const text = ref("")
    const parse = vi.fn(parsePhones)
    const { entries } = mountEntries(text, parse)
    expect(entries.value).toEqual([])
    expect(parse).toHaveBeenCalledTimes(1)

    text.value = "13800138000"
    expect(entries.value).toEqual(["13800138000"])
    text.value = "13800138000\n13900139000"
    expect(entries.value).toEqual(["13800138000", "13900139000"])
    expect(parse).toHaveBeenCalledTimes(3)
  })

  it("超过同步阈值的大文本粘贴进入 300ms 防抖，逐键不再全量解析", async () => {
    const text = ref("")
    const parse = vi.fn(parsePhones)
    const { entries } = mountEntries(text, parse)
    expect(parse).toHaveBeenCalledTimes(1)

    // 5 万行粘贴（每行 11 位号码 + 换行 ≈ 60 万字符）：进入防抖路径且窗口内不解析
    const pasted = Array.from({ length: 50_000 }, (_, index) => `139${String(index).padStart(8, "0")}`).join("\n")
    text.value = pasted
    expect(parse).toHaveBeenCalledTimes(1)
    expect(entries.value).toEqual([])

    // 防抖窗口内继续键入（模拟逐键/分片输入，每次整体替换文本）：只重置定时器，不新增解析
    for (let index = 0; index < 5; index += 1) {
      text.value = `${pasted}\n1380000000${index}`
      expect(parse).toHaveBeenCalledTimes(1)
    }

    await vi.advanceTimersByTimeAsync(299)
    expect(parse).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(parse).toHaveBeenCalledTimes(2)
    expect(entries.value).toHaveLength(50_001)
  })

  it("防抖窗口内 flush 立即落盘最新解析，且定时器不再重复落盘", async () => {
    const text = ref("")
    const parse = vi.fn(parsePhones)
    const { entries, flush } = mountEntries(text, parse)

    text.value = Array.from({ length: 300 }, (_, index) => `139${String(index).padStart(8, "0")}`).join("\n")
    expect(parse).toHaveBeenCalledTimes(1)

    flush()
    expect(parse).toHaveBeenCalledTimes(2)
    expect(entries.value).toHaveLength(300)

    await vi.advanceTimersByTimeAsync(600)
    expect(parse).toHaveBeenCalledTimes(2)
  })

  it("无待定解析时 flush 为空操作", () => {
    const text = ref("13800138000")
    const parse = vi.fn(parsePhones)
    const { entries, flush } = mountEntries(text, parse)
    expect(parse).toHaveBeenCalledTimes(1)

    flush()
    expect(parse).toHaveBeenCalledTimes(1)
    expect(entries.value).toEqual(["13800138000"])
  })

  it("作用域销毁后待定防抖定时器被清理，不再落盘", async () => {
    const text = ref("")
    const parse = vi.fn(parsePhones)
    const { scope } = mountEntries(text, parse)

    text.value = Array.from({ length: 300 }, (_, index) => `139${String(index).padStart(8, "0")}`).join("\n")
    expect(parse).toHaveBeenCalledTimes(1)

    scope.stop()
    await vi.advanceTimersByTimeAsync(600)
    expect(parse).toHaveBeenCalledTimes(1)
  })
})
