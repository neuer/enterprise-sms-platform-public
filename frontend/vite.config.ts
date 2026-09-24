import { defineConfig } from "vitest/config"
import vue from "@vitejs/plugin-vue"

export default defineConfig({
  base: "/",
  plugins: [vue()],
  build: {
    // 字体禁止内联为 data: URL：CSP font-src 'self' 不含 data: 来源，
    // Noto Sans SC Variable 小切片一旦被内联会被浏览器整页拦截。
    assetsInlineLimit(filePath: string): boolean | undefined {
      if (/\.(?:woff2?|ttf|otf|eot)(?:[?#].*)?$/i.test(filePath)) return false
      return undefined
    },
    rollupOptions: {
      output: {
        // vue 家族独立 vendor chunk（缓存稳定）；element-plus 不手工归并——
        // 两级注册后由构建器按可达性自动拆分，入口只带登录片，工作区片随
        // element-workspace 懒加载；强行合并会把全量组件重新拖回登录路径。
        manualChunks(id: string): string | undefined {
          if (/node_modules[\\/](?:vue|vue-router|pinia|@vue)[\\/]/.test(id)) return "vendor"
          return undefined
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    exclude: ["node_modules/**", "dist/**"],
    setupFiles: ["./tests/setup-session.ts"],
    testTimeout: 15000,
  },
})
