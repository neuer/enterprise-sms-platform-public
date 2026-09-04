// ESLint flat config：Vue 3 + TypeScript + 项目单点约定的可自动拦截部分。
// 格式化职责归 Prettier（.prettierrc.json），此处以 eslint-config-prettier 关闭冲突规则。
// 采纳期原则：不上 strictTypeChecked，既有代码只增项目约定拦截，不为历史风格负债制造噪音。
import eslintConfigPrettier from "eslint-config-prettier"
import pluginVue from "eslint-plugin-vue"
import globals from "globals"
import tseslint from "typescript-eslint"

// 各视图禁止的自建实现（单点约定见 AGENTS.md「前端 UI 约定」）；views 块与 src 块
// 的 no-restricted-syntax 不合并（同名规则后者覆盖前者），views 块需重复列全量。
const FETCH_SINGLE_POINT = {
  selector: "CallExpression[callee.name='fetch']",
  message: "禁止直接调用 fetch：统一走 src/api/client.ts 请求基建（pre-auth 例外为 src/api/auth.ts）。",
}
const ROUTER_SINGLE_POINT = {
  selector: "CallExpression[callee.name='getCurrentInstance']",
  message: "禁止经 getCurrentInstance 取 globalProperties.$router；路由一律使用 vue-router 的 useRouter()。",
}
const INTL_SINGLE_POINT = {
  selector: "NewExpression[callee.object.name='Intl'][callee.property.name='DateTimeFormat']",
  message: "时间格式化统一使用 src/lib/time.ts 单点（formatDateTime/formatHms 等），禁止页面自建 Intl.DateTimeFormat。",
}
const CLIPBOARD_SINGLE_POINT = {
  selector: "MemberExpression[object.name='navigator'][property.name='clipboard']",
  message:
    "剪贴板写入统一走 src/lib/clipboard.ts 的 copyText（含非安全上下文回退），禁止页面直接访问 navigator.clipboard。",
}

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  {
    files: ["**/*.vue"],
    languageOptions: {
      // <script setup lang="ts"> 的 script 块交给 ts parser
      parserOptions: { parser: tseslint.parser },
    },
    rules: {
      // .vue 不经 vue-tsc 之外的未使用检查（tsconfig 未开 noUnusedLocals），此处补齐
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  {
    files: ["src/**/*.{ts,vue}", "tests/**/*.ts"],
    languageOptions: {
      globals: { ...globals.browser },
    },
  },
  {
    files: ["tests/**/*.ts"],
    languageOptions: {
      // vitest globals 模式（vite.config.ts test.globals=true）；ts 文件的 no-undef 已由 ts parser 关闭，
      // 这里显式声明只为语义完整。
      globals: {
        ...globals.node,
        suite: "readonly",
        test: "readonly",
        describe: "readonly",
        it: "readonly",
        expect: "readonly",
        assert: "readonly",
        vi: "readonly",
        beforeAll: "readonly",
        afterAll: "readonly",
        beforeEach: "readonly",
        afterEach: "readonly",
      },
    },
  },
  {
    files: ["*.{js,ts}"],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
  {
    // 请求基建与路由单点：src 全域拦截
    files: ["src/**/*.{ts,vue}"],
    rules: {
      "no-restricted-syntax": ["error", FETCH_SINGLE_POINT, ROUTER_SINGLE_POINT],
    },
  },
  {
    // 时间格式化与剪贴板单点：视图层拦截（lib/ 对应单点文件不受影响）
    files: ["src/views/**/*.vue"],
    rules: {
      "no-restricted-syntax": [
        "error",
        FETCH_SINGLE_POINT,
        ROUTER_SINGLE_POINT,
        INTL_SINGLE_POINT,
        CLIPBOARD_SINGLE_POINT,
      ],
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/styles/workspace.css"],
              message: "workspace.css 由 App.vue 全局引入，视图禁止重复 import。",
            },
          ],
        },
      ],
    },
  },
  {
    // fetch 单点的两个例外实现
    files: ["src/api/client.ts", "src/api/auth.ts"],
    rules: {
      "no-restricted-syntax": "off",
    },
  },
  {
    rules: {
      // 采纳期噪音治理：以下 vue 格式系规则与现状大量冲突（模板刻意长行排版），
      // 格式化收口由 Prettier 负责，lint 不再重复执法。
      "vue/html-indent": "off",
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/html-self-closing": "off",
      "vue/html-closing-bracket-newline": "off",
      "vue/first-attribute-linebreak": "off",
      // 历史代码下划线占位解构（如 const { operations: _operations, ...rest }）属刻意为之
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  eslintConfigPrettier,
)
