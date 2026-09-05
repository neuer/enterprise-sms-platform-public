import "element-plus/theme-chalk/el-alert.css"
import "element-plus/theme-chalk/el-card.css"
import "element-plus/theme-chalk/el-checkbox.css"
import "element-plus/theme-chalk/el-date-picker-panel.css"
import "element-plus/theme-chalk/el-descriptions.css"
import "element-plus/theme-chalk/el-dialog.css"
import "element-plus/theme-chalk/el-drawer.css"
import "element-plus/theme-chalk/el-form.css"
import "element-plus/theme-chalk/el-input-number.css"
import "element-plus/theme-chalk/el-loading.css"
import "element-plus/theme-chalk/el-message-box.css"
import "element-plus/theme-chalk/el-overlay.css"
import "element-plus/theme-chalk/el-pagination.css"
import "element-plus/theme-chalk/el-popover.css"
import "element-plus/theme-chalk/el-popper.css"
import "element-plus/theme-chalk/el-radio.css"
import "element-plus/theme-chalk/el-scrollbar.css"
import "element-plus/theme-chalk/el-segmented.css"
import "element-plus/theme-chalk/el-select.css"
import "element-plus/theme-chalk/el-skeleton.css"
import "element-plus/theme-chalk/el-switch.css"
import "element-plus/theme-chalk/el-table.css"
import "element-plus/theme-chalk/el-tabs.css"
import "element-plus/theme-chalk/el-tag.css"
import "element-plus/theme-chalk/el-time-picker.css"
import "element-plus/theme-chalk/el-tooltip.css"
import "element-plus/theme-chalk/el-upload.css"

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
  ElRadioButton,
  ElRadioGroup,
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
    ElRadioButton,
    ElRadioGroup,
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
