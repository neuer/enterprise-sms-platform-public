import { describe, expect, it } from "vitest"

import {
  DEMO_BIZ_ID,
  DEMO_LANGUAGES,
  DEMO_MOBILES,
  buildDemoScript,
  exampleParamsFor,
  paramsLiteral,
  payloadJson,
  type DemoContext,
} from "../src/lib/demoScript"

const context: DemoContext = {
  app: { id: 7, name: "业务通知" } as DemoContext["app"],
  templateId: 42,
  templateName: "登录验证码",
  templateContent: "验证码{1}，{2}分钟内有效",
  params: ["示例参数1", "示例参数2"],
}

describe("demoScript 接入示例生成", () => {
  it("exampleParamsFor 按 pos 排序且不超 max_len，逐级回退到精确填充", () => {
    const params = exampleParamsFor([
      { pos: 2, max_len: 6 },
      { pos: 1, max_len: 2 },
      { pos: 3, max_len: 1 },
    ])
    // pos 升序；pos1 max_len=2 时「示例参数1/参数1」超长→「示例」恰好；pos3 max_len=1 只有「值」恰好
    expect(params).toEqual(["示例", "示例参数2", "值"])
    expect(params[0].length).toBeLessThanOrEqual(2)
    expect(params[1].length).toBeLessThanOrEqual(6)
    expect(params[2].length).toBeLessThanOrEqual(1)
  })

  it("paramsLiteral 按语言生成合法数组字面量", () => {
    expect(paramsLiteral("curl", ["a", "b"])).toBe('["a", "b"]')
    expect(paramsLiteral("python", ["a"])).toBe('["a"]')
    expect(paramsLiteral("node", ["a"])).toBe('["a"]')
    expect(paramsLiteral("java", ["a", "b"])).toBe('new String[]{"a", "b"}')
    expect(paramsLiteral("go", ["a"])).toBe('[]string{"a"}')
    expect(paramsLiteral("php", ["a", "b"])).toBe("['a', 'b']")
  })

  it("payloadJson 固定 notice 类别与掩码号码，不携带明文手机号", () => {
    const payload = JSON.parse(payloadJson(context))
    expect(payload).toEqual({
      category: "notice",
      mobiles: DEMO_MOBILES,
      template_id: 42,
      template_params: ["示例参数1", "示例参数2"],
      biz_id: DEMO_BIZ_ID,
    })
    expect(JSON.stringify(payload)).not.toMatch(/1\d{10}/)
  })

  it.each([...DEMO_LANGUAGES])("%s：API Key 只走环境变量，警告与模板信息齐全", (language) => {
    const script = buildDemoScript(language, context)
    expect(script).toContain("厂商人工审核")
    expect(script).toContain("SMS_API_KEY")
    expect(script).toContain("模板：登录验证码（id=42）")
    expect(script).toContain("模板内容：验证码{1}，{2}分钟内有效")
    // 不出现明文手机号与疑似硬编码 Key
    expect(script).not.toMatch(/1\d{10}/)
    expect(script).not.toMatch(/X-Api-Key["']?\s*[:=]\s*["'][A-Za-z0-9]{16,}/)
  })

  it("无模板内容时省略内容行", () => {
    const script = buildDemoScript("python", { ...context, templateContent: "" })
    expect(script).not.toContain("// 模板内容：")
    expect(script).toContain("requests.post(")
  })
})
