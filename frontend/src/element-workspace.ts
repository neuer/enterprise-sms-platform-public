// 工作区样式随本模块懒加载：登录/公开页只带入口样式，进入首个非公开路由前由 main.ts
// 守卫加载本模块。两份样式各自进层（element 层 / 壳与页面各层），层顺序由入口的
// styles/layers.css 先行声明，懒加载到达的先后不影响级联结果。
import "./styles/element-workspace.css"
import "./styles/workspace.css"

import {
  ElAlert,
  ElCard,
  ElCheckbox,
  ElCheckboxGroup,
  ElDatePicker,
  ElDescriptions,
  ElDescriptionsItem,
  ElDialog,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInputNumber,
  ElLoading,
  ElOption,
  ElPagination,
  ElPopover,
  ElSegmented,
  ElSelect,
  ElSkeleton,
  ElSwitch,
  ElTabPane,
  ElTable,
  ElTableColumn,
  ElTabs,
  ElTag,
  ElTooltip,
  ElUpload,
} from "element-plus"
import type { App } from "vue"

/**
 * 认证工作区的 Element Plus 组件与样式：登录/公开页只带 main.ts 最小集，
 * 进入首个非公开路由前由 main.ts 的路由守卫动态 import 本模块并完成注册。
 * app.use 在挂载后调用仍然有效（组件在渲染时解析）；本模块只会被执行一次，
 * 重复调用注册是幂等的。
 */
export function registerWorkspaceElement(app: App): void {
  for (const plugin of [
    ElAlert,
    ElCard,
    ElCheckbox,
    ElCheckboxGroup,
    ElDatePicker,
    ElDescriptions,
    ElDescriptionsItem,
    ElDialog,
    ElDrawer,
    ElForm,
    ElFormItem,
    ElInputNumber,
    ElLoading,
    ElOption,
    ElPagination,
    ElPopover,
    ElSegmented,
    ElSelect,
    ElSkeleton,
    ElSwitch,
    ElTabPane,
    ElTable,
    ElTableColumn,
    ElTabs,
    ElTag,
    ElTooltip,
    ElUpload,
  ])
    app.use(plugin)
}
