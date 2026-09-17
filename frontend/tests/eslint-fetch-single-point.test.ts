import { resolve } from "node:path"

import { ESLint } from "eslint"

/**
 * fetch 单点拦截（eslint.config.js 的 FETCH_SINGLE_POINT）行为测试：
 * 直接 fetch(...) 与 window.fetch / globalThis.fetch（含计算属性形式）都必须被拦截，
 * 请求基建例外文件（src/api/client.ts 等）不受影响。
 */

const cwd = resolve(process.cwd())
const eslint = new ESLint({ cwd })

const FETCH_RULE = "no-restricted-syntax"

async function restrictedMessages(code: string, filePath: string) {
  const [result] = await eslint.lintText(code, { filePath: resolve(cwd, filePath) })
  return result.messages.filter((message) => message.ruleId === FETCH_RULE)
}

describe("fetch 单点 ESLint 拦截", () => {
  it("拦截裸 fetch(...) 调用", async () => {
    const messages = await restrictedMessages(`fetch("/api/v1/x")`, "src/probe-fetch.ts")
    expect(messages).toHaveLength(1)
    expect(messages[0].message).toContain("禁止直接调用 fetch")
  })

  it("拦截 window.fetch / globalThis.fetch 成员调用", async () => {
    for (const callee of ["window.fetch", "globalThis.fetch", 'window["fetch"]', 'globalThis["fetch"]']) {
      const messages = await restrictedMessages(`${callee}("/api/v1/x")`, "src/probe-fetch.ts")
      expect(messages, `${callee} 必须被拦截`).toHaveLength(1)
      expect(messages[0].message).toContain("禁止直接调用 fetch")
    }
  })

  it("不拦截请求基建与无关成员调用", async () => {
    const ok = await restrictedMessages(
      `import { apiRequest } from "./client"\nwindow.fetchSomething("/x")\napiRequest("/x", { method: "GET" })`,
      "src/probe-fetch.ts",
    )
    expect(ok).toHaveLength(0)
  })

  it("请求基建例外文件（client/auth/httpDeadline）不受拦截", async () => {
    for (const filePath of ["src/api/client.ts", "src/api/auth.ts", "src/api/httpDeadline.ts"]) {
      const messages = await restrictedMessages(`fetch("/api/v1/x")\nwindow.fetch("/api/v1/y")`, filePath)
      expect(messages, `${filePath} 为请求基建例外`).toHaveLength(0)
    }
  })
})
