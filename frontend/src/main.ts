import "@fontsource/ibm-plex-mono/400.css"
import "@fontsource/ibm-plex-mono/500.css"
import "@fontsource/ibm-plex-mono/600.css"
import "@fontsource-variable/noto-sans-sc/index.css"
import "@fontsource-variable/noto-serif-sc/index.css"
import "element-plus/theme-chalk/base.css"
import "element-plus/theme-chalk/el-alert.css"
import "element-plus/theme-chalk/el-button.css"
import "element-plus/theme-chalk/el-card.css"
import "element-plus/theme-chalk/el-checkbox.css"
import "element-plus/theme-chalk/el-config-provider.css"
import "element-plus/theme-chalk/el-date-picker-panel.css"
import "element-plus/theme-chalk/el-descriptions.css"
import "element-plus/theme-chalk/el-dialog.css"
import "element-plus/theme-chalk/el-drawer.css"
import "element-plus/theme-chalk/el-empty.css"
import "element-plus/theme-chalk/el-form.css"
import "element-plus/theme-chalk/el-icon.css"
import "element-plus/theme-chalk/el-input.css"
import "element-plus/theme-chalk/el-input-number.css"
import "element-plus/theme-chalk/el-loading.css"
import "element-plus/theme-chalk/el-message.css"
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
import "./styles/theme.css"

import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElCheckboxGroup,
  ElConfigProvider,
  ElDatePicker,
  ElDescriptions,
  ElDescriptionsItem,
  ElDialog,
  ElDrawer,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
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
import { createPinia } from "pinia"
import { createApp } from "vue"

import App from "./App.vue"
import router, { installAuthGuard } from "./router"
import { useSessionStore } from "./stores/session"

const pinia = createPinia()
useSessionStore(pinia).restore()
// 守卫会等待这次恢复；restoreFromCookie 内部消化所有异常，只以布尔值收敛。
const sessionReady = useSessionStore(pinia).restoreFromCookie()
installAuthGuard(router, pinia, sessionReady)

const application = createApp(App).use(pinia).use(router)
for (const plugin of [
  ElAlert, ElButton, ElCard, ElCheckbox, ElCheckboxGroup, ElConfigProvider,
  ElDatePicker, ElDescriptions, ElDescriptionsItem, ElDialog, ElDrawer, ElEmpty,
  ElForm, ElFormItem, ElInput, ElInputNumber, ElLoading, ElOption, ElPagination,
  ElPopover, ElRadioButton, ElRadioGroup, ElSegmented, ElSelect, ElSkeleton,
  ElSwitch, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag, ElTooltip, ElUpload,
]) application.use(plugin)
application.mount("#app")
