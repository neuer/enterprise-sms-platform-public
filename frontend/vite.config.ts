import { defineConfig } from "vitest/config"
import vue from "@vitejs/plugin-vue"

export default defineConfig({
  base: "/",
  plugins: [vue()],
  test: {
    environment: "jsdom",
    globals: true,
    exclude: ["node_modules/**", "dist/**"],
    setupFiles: ["./tests/setup-session.ts"],
    testTimeout: 15000,
  },
})
